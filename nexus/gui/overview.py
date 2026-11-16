"""The game page's stat tiles (w-132): Players, Critics, How long to beat, On
Linux, PC features, Details and the site links, in a fixed order.

overview_model() is plain data (no Qt, no OS call: `linux` comes from the
caller); OverviewTiles shows it. Colours are palette role names, resolved
when painted or styled, so a theme change applies."""

from PyQt6.QtCore import pyqtSignal, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import (QColor, QDesktopServices, QFont, QImageReader, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import (QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget)

from nexus import categories, facts

from .art import _initials, ART, logo_lightness
from .gameinfo import PCGW_COLORS, PCGW_WORDS, TIER_COLORS
from .theme import _mix, FONTS, G, is_light, P, R, scaled, SP
from .widgets import clear_layout, FlowLayout, set_icon_label

ORDER = ('players', 'critics', 'length', 'linux', 'pc', 'details', 'studio', 'links')
TITLES = {'players': "Players", 'critics': "Critics", 'length': "How long to beat",
          'linux': "On Linux", 'pc': "PC features", 'details': "Details", 'studio': "Studio", 'links': ""}
# Tiles that share a row when the panel is wide enough.
PAIRS = (('players', 'critics'), ('linux', 'pc'))
FS_INITIALS, FS_CHEVRON = 15, 16      # a studio's initials, the row's chevron (px at scale 1)
MODE_LABELS = {'single': "Single", 'coop': "Co-op", 'online': "Online", 'local': "Couch"}
PC_MAX = 5
ART_TRACK = '#4a4f5e'                # empty ring / bar on Theatre's dark art
TILE_W = 220                        # a Theatre tile's width at scale 1 (w-135)
PAIR_MIN_W = 460 - 2 * SP['5']       # panel width under which a pair stacks
SITES = (("Steam store", 'https://store.steampowered.com/'),
         ("ProtonDB", 'https://www.protondb.com/'),
         ("PCGamingWiki", 'https://www.pcgamingwiki.com/'),
         ("HowLongToBeat", 'https://howlongtobeat.com/'))


# --- Facts -------------------------------------------------------------------
# The one record both the desktop page and Theatre's drawer build from.

_FACTS: dict = {'mtime': None, 'games': {}}


def game_facts(slug: str | None) -> dict | None:
    """A library game's record in the facts index. The CLI writes the index
    (--game-facts, the agent); the window only reads it, again when the
    file changes."""
    if not slug:
        return None
    try:
        mtime = facts.index_path().stat().st_mtime
    except OSError:
        return None
    if mtime != _FACTS['mtime']:
        _FACTS.update(mtime=mtime, games=facts.read_index().get('games') or {})
    rec = _FACTS['games'].get(slug)
    return rec if isinstance(rec, dict) else None


def credits_text(rec: dict) -> tuple[str, str]:
    """("FromSoftware · Bandai Namco · 2022", full text for the tooltip);
    the publisher is dropped when it is the developer."""
    devs = [d for d in rec.get('developers') or [] if d]
    pubs = [p for p in rec.get('publishers') or [] if p]
    dev = devs[0] if devs else ''
    pub = next((p for p in pubs if p.casefold() not in {d.casefold() for d in devs}), '')
    year = str(rec['year']) if rec.get('year') else ''
    short = " · ".join(x for x in (dev, pub, year) if x)
    full = "\n".join(x for x in (
        ("Developer: " if len(devs) == 1 else "Developers: ") + ", ".join(devs) if devs else '',
        ("Publisher: " if len(pubs) == 1 else "Publishers: ") + ", ".join(pubs) if pubs else '',
        f"Released: {rec.get('releaseText') or rec.get('release')}"
        if rec.get('releaseText') or rec.get('release') else '') if x)
    return short, full


def overview_facts(game: dict | None, store: dict | None, tips: dict | None) -> dict:
    """The game's facts: the index first; else what --store-info brought (a
    game the index has not reached yet); the play length from --game-tips
    (HowLongToBeat, else Game-db's) when the index has none."""
    game, tips = game or {}, tips or {}
    rec = game_facts(game.get('slug'))
    st = (store or {}).get('store')
    if rec is None and st:
        rec = facts.merge({'appid': game.get('appid'), 'name': game.get('display_name'),
                           'steam': facts.from_steam(st), 'hltb': None})
    gd = (tips.get('gamedb') or {}).get('hltb') or {}
    live = tips.get('hltb') or (gd if gd.get('main') else None) or {}
    if live.get('main') and not (rec or {}).get('length'):
        rec = {**(rec or {}), 'length': {**live, 'source': live.get('source') or 'hltb'}}
    return rec or {}


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


LOGO_LIGHT = 0.6            # a logo this light needs a dark plate
IGDB_MIN_CRITICS = 3        # fewer critics than this is not a score worth a badge


def _critics(st: dict, rec: dict) -> list:
    """Metacritic, then IGDB's critics (w-133), always in that order."""
    out = []
    score = _number((st.get('metacritic') or {}).get('score')) or _number(rec.get('metacritic'))
    if score:
        v = int(score)
        out.append({'name': "Metacritic", 'value': v, 'word': critic_word(v),
                    'tone': score_tone(v)})
    igdb, n = _number(rec.get('igdbRating')), _number(rec.get('igdbRatingCount'))
    if igdb and n is not None and n >= IGDB_MIN_CRITICS:
        v = round(igdb)
        out.append({'name': "IGDB", 'value': v, 'word': critic_word(v), 'tone': score_tone(v)})
    return out


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


def studio_credits(rec: dict, ok=None, strict: bool = False) -> list[dict]:
    """The credits a game page shows as studios (w-114, w-134): the developer,
    then the first publisher that is another studio. [{role 'dev'|'pub', key,
    name (as credited), both}] (`both`: a developer that also publishes it).
    `ok(key)` says whether that studio has a page: strict keeps only those
    that do (the chips); otherwise a studio with a page is preferred and the
    first credit is the fallback (the tile). Pure."""
    def names(field):
        return [n for n in rec.get(field) or [] if isinstance(n, str)
                and categories.studio_id(n)]

    def pick(role, field, skip=''):
        pool = [n for n in names(field) if categories.studio_id(n) != skip]
        if ok is not None:
            for n in pool:
                if ok(categories.studio_key(role, n)):
                    return n
            if strict:
                return None
        return pool[0] if pool else None

    out = []
    dev = pick('dev', 'developers')
    dev_id = categories.studio_id(dev) if dev else ''
    if dev:
        out.append({'role': 'dev', 'key': categories.studio_key('dev', dev), 'name': dev,
                    'both': any(categories.studio_id(p) == dev_id for p in names('publishers'))})
    pub = pick('pub', 'publishers', dev_id)
    if pub:
        out.append({'role': 'pub', 'key': categories.studio_key('pub', pub), 'name': pub,
                    'both': False})
        if out[0]['role'] == 'dev':
            out[0]['both'] = False       # another publisher is named: shown on its own row
    return out


def _studio_logo(rec: dict, credit: dict) -> str | None:
    """The IGDB logo of a credited studio: the Game-db company with the same
    studio id (the same role first), else, when each side names exactly one
    studio in that role, that one."""
    role = {'dev': 'developer', 'pub': 'publisher'}[credit['role']]
    sid = categories.studio_id(credit['name'])
    entries = [s for s in rec.get('studios') or [] if isinstance(s, dict)]
    same = [s for s in entries if categories.studio_id(s.get('name')) == sid]
    hit = next((s for s in same if s.get('role') == role), None) or (same[0] if same else None)
    if hit is None:
        mine = [s for s in entries if s.get('role') == role]
        credited = [n for n in rec.get({'dev': 'developers', 'pub': 'publishers'}[credit['role']])
                    or [] if isinstance(n, str)]
        if len(mine) == 1 and len(credited) == 1:
            hit = mine[0]
    logo = (hit or {}).get('logo')
    return logo if isinstance(logo, str) and logo.startswith(facts.LOGO_HOST) else None


def _studio(rec: dict, counts) -> list:
    """The Studio tile's rows: {key, name, role developer|publisher, n (your
    games), open (its Library page exists), logo, initials, both}."""
    ok = (lambda k: counts.get(k, 0) >= categories.STUDIO_MIN) if counts is not None else None
    rows = []
    for c in studio_credits(rec, ok):
        n = int((counts or {}).get(c['key'], 0))
        rows.append({'key': c['key'], 'name': c['name'], 'both': c['both'],
                     'role': 'developer' if c['role'] == 'dev' else 'publisher', 'n': n,
                     'open': counts is not None and n >= categories.STUDIO_MIN,
                     'logo': _studio_logo(rec, c),
                     'initials': _initials(categories.studio_name(c['name']))[:2]})
    return rows


def logo_plate(lightness, light: bool) -> str:
    """The palette role a studio logo sits on so it shows: a light logo gets a
    dark plate, a dark one (or an unknown) a light plate, on either theme."""
    if lightness is not None and lightness >= LOGO_LIGHT:
        return 'text' if light else 'surface_2'
    return 'surface' if light else 'text'


def _links(st: dict, tips: dict, linux: bool) -> list:
    out = []
    for (name, site), url in zip(SITES, (
            st.get('url'), (tips.get('protondb') or {}).get('url') if linux else None,
            (tips.get('pcgw') or {}).get('url'), (tips.get('hltb') or {}).get('url'))):
        if str(url or '').startswith(site):
            out.append({'name': name, 'url': str(url)})
    return out


def overview_model(rec, store, tips, game, linux: bool = True,
                   studio_counts=None) -> list[dict]:
    """[{key, title, items}] in ORDER, each section only when its facts exist.
    `rec` is the facts index record, `store` --store-info's answer, `tips`
    --game-tips', `game` the library entry; `linux` says whether Linux-only
    parts (ProtonDB, Deck, anti-cheat) belong; `studio_counts` is
    categories.studio_counts' {studio key: your games} (the Studio tile's
    numbers and which rows open a page)."""
    rec, store, tips, game = rec or {}, store or {}, tips or {}, game or {}
    st = store.get('store') or {}
    items = {
        'players': _players(store.get('reviews') or {}),
        'critics': _critics(st, rec),
        'length': _length(rec, game),
        'linux': _linux(tips, game) if linux else [],
        'pc': _pc(tips),
        'details': _details(rec),
        'studio': _studio(rec, studio_counts),
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


def _track(ink: str) -> QColor:
    """The empty part of a ring or bar: a surface, or on Theatre's dark art
    (Dawn's surfaces are cream there) a fixed slate."""
    return _tone('surface_2') if ink == 'text' else QColor(ART_TRACK)


def _bold(widget: QWidget, px: float, scale: float = 1.0) -> QFont:
    f = QFont(widget.font())
    f.setFamily(FONTS.ui)
    f.setPixelSize(scaled(px * scale))
    f.setWeight(QFont.Weight.DemiBold)
    return f


class ScoreRing(QWidget):
    """A percentage as a ring with the number inside (Steam reviews)."""
    SIZE, PEN = 60, 6

    def __init__(self, parent=None, scale: float = 1.0, ink: str = 'text'):
        super().__init__(parent)
        self.value, self.tone, self.scale, self.ink = 0, 'success', scale, ink
        self.setFixedSize(scaled(self.SIZE * scale), scaled(self.SIZE * scale))

    def set_score(self, value: int, tone: str, name: str):
        self.value, self.tone = max(0, min(100, int(value))), tone
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = float(scaled(self.PEN * self.scale))
        side = float(min(self.width(), self.height())) - pen
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        pt.setPen(QPen(_track(self.ink), pen))
        pt.drawEllipse(rect)
        arc = QPen(_tone(self.tone), pen)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        pt.setPen(arc)
        pt.drawArc(rect, 90 * 16, -int(self.value / 100 * 360 * 16))
        pt.setPen(_tone(self.ink))
        pt.setFont(_bold(self, 16, self.scale))
        pt.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), str(self.value))
        pt.end()


class ScoreBadge(QWidget):
    """A critic score in a soft square tinted by how good it is; the tint
    gets stronger with the score (and a little more on a light theme)."""
    SIZE = 44

    def __init__(self, parent=None, scale: float = 1.0, ink: str = 'text'):
        super().__init__(parent)
        self.value, self.tone, self.scale, self.ink = 0, 'success', scale, ink
        self.setFixedSize(scaled(self.SIZE * scale), scaled(self.SIZE * scale))

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
        r = float(R['control']) * self.scale
        pt.drawRoundedRect(box, r, r)
        pt.setPen(_tone(self.tone))
        pt.setFont(_bold(self, 18, self.scale))
        pt.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), str(self.value))
        pt.end()


class TierBadge(QWidget):
    """ProtonDB's tier as a small square in its own colour, with its letter."""
    SIZE = 28

    def __init__(self, parent=None, scale: float = 1.0, ink: str = 'text'):
        super().__init__(parent)
        self.tier, self.scale, self.ink = '', scale, ink
        self.setFixedSize(scaled(self.SIZE * scale), scaled(self.SIZE * scale))

    def set_tier(self, tier: str, name: str):
        self.tier = tier
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(TIER_COLORS[self.tier]) if self.tier in TIER_COLORS else (
            _tone('muted') if self.ink == 'text' else _tone(self.ink, 0.6))
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
        r = float(R['sm']) * self.scale
        pt.drawRoundedRect(box, r, r)
        pt.setPen(c)
        pt.setFont(_bold(self, 13, self.scale))
        pt.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter),
                    (self.tier[:1] or '?').upper())
        pt.end()


class LengthBar(QWidget):
    """Main story, with extras and everything on one bar, in three shades
    of the accent, and a dot where you are."""
    H, DOT = 10, 14

    def __init__(self, parent=None, scale: float = 1.0, ink: str = 'text'):
        super().__init__(parent)
        self.main = self.plus = self.complete = self.you = None
        self.scale, self.ink = scale, ink
        self.setFixedHeight(scaled(self.DOT * scale) + 2)
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
        h, w = float(scaled(self.H * self.scale)), float(self.width())
        y = (self.height() - h) / 2
        r = h / 2
        pt.setPen(Qt.PenStyle.NoPen)
        for value, shade in ((None, 0.0), (self.complete, 0.3), (self.plus, 0.65),
                             (self.main, 1.0)):
            frac = 1.0 if value is None else min(1.0, value / top) if value else 0.0
            if frac <= 0:
                continue
            base = _track(self.ink)
            col = base if value is None else QColor(_mix(
                base.name(), P['accent'], shade))
            pt.setBrush(col)
            pt.drawRoundedRect(QRectF(0.0, y, max(h, w * frac), h), r, r)
        if self.you:
            d = float(scaled(self.DOT * self.scale))
            x = min(max(w * self.you / top, d / 2), w - d / 2)
            pt.setBrush(QColor(P['bg']) if self.ink == 'text' else QColor(0, 0, 0, 170))
            pen = QPen(_tone(self.ink), float(scaled(3 * self.scale)))
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

    def __init__(self, parent=None, ink: str = 'text', name: str = "OvSub",
                 role: str = 'muted'):
        super().__init__(parent)
        self.ink, self.role = ink, role
        self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setFont(self.font())
        pt.setPen(_tone(self.role) if self.ink == 'text' else _tone(self.ink, 0.7))
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

    def __init__(self, key: str, parent=None, theatre: bool = False, scale: float = 1.0):
        super().__init__(parent)
        self.key = key
        # Theatre (w-135): a fixed width, a focus stop, `scale` times as big,
        # and its text and painted parts in on_art (the art is dark).
        self.theatre, self.scale = theatre, scale if theatre else 1.0
        self.ink = 'on_art' if theatre else 'text'
        self.setObjectName("OvTile")
        if theatre:
            self.setFixedWidth(round(TILE_W * self.scale))
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(self.sp(SP['4']), self.sp(SP['3']), self.sp(SP['4']),
                                    self.sp(SP['3']))
        self.lay.setSpacing(self.sp(SP['2']))
        head = QHBoxLayout()
        head.setSpacing(self.sp(SP['1_5']))
        if self.ICON:
            ic = QLabel(self)
            set_icon_label(ic, G[self.ICON], round(12 * self.scale),
                           'on_art' if theatre else 'muted')
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

    def sp(self, v) -> int:
        """A spacing or margin at this tile's scale."""
        return round(v * self.scale)

    def tone_icon_role(self, tone: str) -> str:
        """The palette role for a small icon: quiet ones follow the ink."""
        return self.ink if tone == 'muted' else tone

    def fill(self, items):
        raise NotImplementedError


class PlayersTile(Tile):
    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('players', parent, theatre, scale)
        row = QHBoxLayout()
        row.setSpacing(self.sp(SP['3']))
        self.ring = ScoreRing(self, self.scale, self.ink)
        row.addWidget(self.ring)
        col = QVBoxLayout()
        col.setSpacing(self.sp(SP['0_5']))
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
    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('critics', parent, theatre, scale)
        self.row = QHBoxLayout()
        self.row.setSpacing(self.sp(SP['1_5']))
        self.lay.addLayout(self.row)
        self.lay.addStretch(1)

    def fill(self, items):
        clear_layout(self.row)
        for c in items:                       # appended by later sources (w-133)
            col = QVBoxLayout()
            col.setSpacing(self.sp(SP['1']))
            badge = ScoreBadge(self, self.scale, self.ink)
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

    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('length', parent, theatre, scale)
        self.bar = LengthBar(self, self.scale, self.ink)
        self.lay.addWidget(self.bar)
        grid = QGridLayout()
        grid.setHorizontalSpacing(self.sp(SP['2']))
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

    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('linux', parent, theatre, scale)
        self.tier_row = QWidget(self)
        tr = QHBoxLayout(self.tier_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(self.sp(SP['2']))
        self.badge = TierBadge(self.tier_row, self.scale, self.ink)
        tr.addWidget(self.badge)
        self.tier_text = _Elide(self.tier_row, self.ink)
        tr.addWidget(self.tier_text, 1)
        self.lay.addWidget(self.tier_row)
        self.chip_host = QWidget(self)
        self.chips = FlowLayout(self.chip_host, margin=0, spacing=self.sp(SP['1_5']))
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

    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('pc', parent, theatre, scale)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(self.sp(SP['1']))
        self.lay.addLayout(self.rows)
        self.lay.addStretch(1)
        self.setToolTip("From PCGamingWiki")

    def fill(self, items):
        clear_layout(self.rows)
        for it in items:
            row = QHBoxLayout()
            row.setSpacing(self.sp(SP['2']))
            ic = QLabel(self)
            set_icon_label(ic, G[it['glyph']], round(13 * self.scale),
                           self.tone_icon_role(it['tone']))
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

    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('details', parent, theatre, scale)
        self.kv = QGridLayout()
        self.kv.setHorizontalSpacing(self.sp(SP['3']))
        self.kv.setColumnStretch(1, 1)
        self.lay.addLayout(self.kv)
        self.pill_host = QWidget(self)
        self.pills = FlowLayout(self.pill_host, margin=0, spacing=self.sp(SP['1_5']))
        self.lay.addWidget(self.pill_host)
        self.lay.addStretch(1)

    def fill(self, items):
        clear_layout(self.kv)
        clear_layout(self.pills)
        modes = next((i['modes'] for i in items if 'modes' in i), [])
        for r, rel in enumerate(i['release'] for i in items if 'release' in i):
            key = QLabel("Released", self)
            key.setObjectName("OvKey")
            key.setFixedWidth(scaled(84 * self.scale))
            self.kv.addWidget(key, r, 0)
            self.kv.addWidget(_label(rel, "OvValue", self), r, 1)
        for _key, text, on in modes:
            self.pills.addWidget(_pill(text, 'on' if on else 'off', self.pill_host))
            self.pills.itemAt(self.pills.count() - 1).widget().setToolTip(
                f"{text}: {'yes' if on else 'no'}")
        self.pill_host.setVisible(bool(modes))


class LogoSquare(QWidget):
    """A studio's logo on a plate that shows it (light logo: dark plate, dark
    logo: light plate), or its initials when no logo is known. Painted;
    float overloads only."""
    SIZE = 44

    def __init__(self, parent=None, initials: str = ''):
        super().__init__(parent)
        self.initials, self.pm, self.plate = initials, None, 'surface_2'
        self.setFixedSize(scaled(self.SIZE), scaled(self.SIZE))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_logo(self, pm: QPixmap | None, lightness: float | None):
        self.pm = pm if pm is not None and not pm.isNull() else None
        self.plate = logo_plate(lightness, is_light()) if self.pm else 'surface_2'
        self.update()

    def paintEvent(self, ev):
        pt = QPainter(self)
        pt.setRenderHint(QPainter.RenderHint.Antialiasing)
        pt.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        pt.setPen(Qt.PenStyle.NoPen)
        pt.setBrush(QColor(P[self.plate]))
        pt.drawRoundedRect(box, float(R['control']), float(R['control']))
        if self.pm is not None:
            inner = box.adjusted(*(float(scaled(SP['1_5'])),) * 2, *(-float(scaled(SP['1_5'])),) * 2)
            size = self.pm.deviceIndependentSize()
            k = min(inner.width() / max(1.0, size.width()), inner.height() / max(1.0, size.height()))
            w, h = size.width() * k, size.height() * k
            pt.drawPixmap(QRectF(inner.center().x() - w / 2, inner.center().y() - h / 2, w, h),
                          self.pm, QRectF(self.pm.rect()))
        else:
            pt.setPen(QColor(P['subtext']))
            pt.setFont(_bold(self, FS_INITIALS))
            pt.drawText(box, int(Qt.AlignmentFlag.AlignCenter), self.initials)
        pt.end()


class StudioRow(QWidget):
    """One studio: logo, name, "Developer · 3 games in your library" and a
    chevron. A focus stop that opens the studio's page when it has one."""
    clicked = pyqtSignal()

    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.row = row
        self.setObjectName("OvStudioRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP['3'])
        self.logo = LogoSquare(self, row['initials'])
        lay.addWidget(self.logo)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addStretch()
        self.name = _Elide(self, name="OvStudioName", role='text')
        self.name.setText(row['name'])
        self.sub = _Elide(self)
        role = ("Developer & publisher" if row.get('both')
                else row['role'].capitalize())
        n = row['n']
        self.sub.setText(role + (f" · {n} game{'s' if n != 1 else ''} in your library" if n else ''))
        col.addWidget(self.name)
        col.addWidget(self.sub)
        col.addStretch()
        lay.addLayout(col, 1)
        self.chev = QLabel(self)
        set_icon_label(self.chev, G['chevron_right'], FS_CHEVRON, 'muted')
        self.chev.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.chev.setVisible(bool(row['open']))
        lay.addWidget(self.chev)
        for w in (self.name, self.sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMinimumHeight(scaled(LogoSquare.SIZE))
        words = f"{row['name']}, {self.sub.text()}"
        self.setAccessibleName(words + (", opens its page" if row['open'] else ''))
        self.setToolTip(f"{row['name']}'s page in the Library" if row['open'] else words)
        if row['open']:
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False

    def enterEvent(self, ev):
        self._hover = True
        self.update()

    def leaveEvent(self, ev):
        self._hover = False
        self.update()

    def paintEvent(self, ev):
        if self._hover and self.row['open']:
            pt = QPainter(self)
            pt.setRenderHint(QPainter.RenderHint.Antialiasing)
            pt.setPen(Qt.PenStyle.NoPen)
            pt.setBrush(_tone('text', 0.06))
            pt.drawRoundedRect(QRectF(self.rect()), float(R['control']), float(R['control']))
            pt.end()

    def mouseReleaseEvent(self, ev):
        if (self.row['open'] and ev.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(ev.position().toPoint())):
            self.clicked.emit()

    def keyPressEvent(self, ev):
        if self.row['open'] and ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                                             Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(ev)


class StudioTile(Tile):
    """Developer and publisher with their logos (w-134). Logos come through
    the art cache; the tile, not a row, owns what arrives (rows are rebuilt
    by every fill), and a late logo is only used while its URL is still one
    of the rows'."""
    ICON = 'home'
    open_key = pyqtSignal(str)

    def __init__(self, parent=None, theatre=False, scale=1.0):
        super().__init__('studio', parent, theatre, scale)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(self.sp(SP['2_5']))
        self.lay.addLayout(self.rows)
        self.lay.addStretch(1)
        self._rows: list[StudioRow] = []
        self._logos: dict[str, tuple] = {}         # url -> (pixmap, lightness)
        self._asked: set[str] = set()

    def fill(self, items):
        clear_layout(self.rows)
        self._rows = []
        for it in items:
            r = StudioRow(it, self)
            r.clicked.connect(lambda k=it['key']: self.open_key.emit(k))
            self.rows.addWidget(r)
            self._rows.append(r)
            if it.get('logo'):
                self._want(it['logo'])
        self._apply()

    def _want(self, url: str):
        if url in self._logos:
            return
        path = ART.cached(url)
        if path:
            self._arrived(url, path)
        elif url not in self._asked:
            self._asked.add(url)
            ART.fetch(url, lambda p, u=url: self._arrived(u, p))

    def _arrived(self, url: str, path):
        """A logo came off the disk or the net: decode it once, then redraw
        the rows that still want it."""
        try:
            if url not in {r.row.get('logo') for r in self._rows}:
                return
            img = QImageReader(path).read()
            if img.isNull():
                return
            self._logos[url] = (QPixmap.fromImage(img), logo_lightness(path))
            self._apply()
        except RuntimeError:                      # the tile is gone
            pass

    def _apply(self):
        for r in self._rows:
            got = self._logos.get(r.row.get('logo') or '')
            r.logo.set_logo(*(got or (None, None)))


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
    store or tips answer neither reorders anything nor drops focus.

    `theatre` (w-135): the drawer's Overview. One horizontal row of
    fixed-width tiles `scale` times as big, each a focus stop for the pad,
    with no links row (a TV never opens a browser) and no Studio tile (its
    pages are Library pages). `open_key` carries a Studio row's key."""
    open_key = pyqtSignal(str)

    def __init__(self, parent=None, theatre: bool = False, scale: float = 1.0):
        super().__init__(parent)
        self.theatre, self.scale = theatre, scale if theatre else 1.0
        self._items: dict[str, object] = {}
        self._shown: list[str] = []
        if theatre:
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            h = QHBoxLayout(self)
            m = round(SP['2'] * self.scale)            # room for the focus ring
            h.setContentsMargins(m, m, m, m)
            h.setSpacing(round(SP['3'] * self.scale))
            kw = {'theatre': True, 'scale': self.scale}
            self.tile = {'players': PlayersTile(self, **kw), 'critics': CriticsTile(self, **kw),
                         'length': LengthTile(self, **kw), 'linux': LinuxTile(self, **kw),
                         'pc': PcTile(self, **kw), 'details': DetailsTile(self, **kw)}
            self.pairs = []
            for key in ORDER:
                if key in self.tile:
                    h.addWidget(self.tile[key])
            h.addStretch(1)
            for w in self.tile.values():
                w.hide()
            return
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SP['2_5'])
        self.tile = {'players': PlayersTile(self), 'critics': CriticsTile(self),
                     'length': LengthTile(self), 'linux': LinuxTile(self), 'pc': PcTile(self),
                     'details': DetailsTile(self), 'studio': StudioTile(self),
                     'links': LinksRow(self)}
        self.tile['studio'].open_key.connect(self.open_key)
        self.pairs = [TilePair(self.tile[a], self.tile[b], self) for a, b in PAIRS]
        rows = {'players': self.pairs[0], 'length': self.tile['length'],
                'linux': self.pairs[1], 'details': self.tile['details'],
                'studio': self.tile['studio'], 'links': self.tile['links']}
        for key in ORDER:
            if key in rows:
                v.addWidget(rows[key])
        for w in (*self.tile.values(), *self.pairs):
            w.hide()

    def set_sections(self, sections: list[dict]) -> bool:
        """Show `sections` (overview_model's), and the whole block only when
        any tile shows; True then."""
        got = {s['key']: s['items'] for s in sections if s['key'] in self.tile}
        for key, tile in self.tile.items():
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
        self._shown = [k for k in ORDER if got.get(k)]
        self.setVisible(bool(self._shown))
        return bool(self._shown)

    def keys(self) -> list[str]:
        return list(self._shown)

    def row_height(self) -> int:
        """Theatre: the height the row needs (its tallest tile, wrapped at
        the tile's width)."""
        hs = [max(t.sizeHint().height(), t.heightForWidth(t.width()) if t.hasHeightForWidth()
                  else 0) for t in self.tile.values() if not t.isHidden()]
        m = self.layout().contentsMargins()
        return max(hs, default=0) + m.top() + m.bottom()
