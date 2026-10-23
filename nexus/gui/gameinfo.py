"""Facts about a game shown in several places: install/play state, compat."""

import html

from .theme import P


SOURCE_LABEL = {'nexus': 'Repack', 'steam': 'Steam', 'heroic': 'Heroic', 'epic': 'Epic'}
TIER_COLORS = {'platinum': '#b4c7dc', 'gold': '#cfb53b', 'silver': '#a6a6a6',
               'bronze': '#cd7f32', 'borked': '#eb6f92', 'native': '#9ccfd8'}
PCGW_COLORS = {'true': 'success', 'always on': 'success', 'limited': 'warning',
               'hackable': 'warning', 'false': 'danger', 'n/a': 'muted'}
PCGW_WORDS = {'true': 'yes', 'false': 'no', 'hackable': 'with a fix', 'always on': 'always on',
              'limited': 'limited', 'n/a': 'n/a'}


def anticheat_warning(tips: dict) -> str | None:
    """One line when Are We Anti-Cheat Yet says online play won't work."""
    ac = (tips or {}).get('anticheat') or {}
    if ac.get('status') not in ('Broken', 'Denied'):
        return None
    what = ', '.join(ac.get('anticheats') or []) or 'its anti-cheat'
    verb = "is blocked by the developer" if ac['status'] == 'Denied' else "doesn't run"
    return f"Online play: {what} {verb} on Linux — multiplayer likely won't work."


def compat_facts(tips: dict) -> tuple[list[tuple[str, str | None, bool]], str]:
    """(lines, rich-text detail) for Steam Deck, anti-cheat and
    PCGamingWiki facts. Lines are (text, colour, bold) like the Theatre
    cards; the detail escapes everything and links only https URLs."""
    tips = tips or {}
    lines, more = [], []
    dk = tips.get('deck') or {}
    if dk.get('deck'):
        lines.append((f"Steam Deck {dk['deck']}", {'Verified': P['success'], 'Playable':
                      P['warning'], 'Unsupported': P['danger']}.get(dk['deck']), False))
        more.append(f"<p><b>Steam Deck</b>: {html.escape(dk['deck'])}"
                    + (f" · SteamOS {html.escape(dk['steamos'])}" if dk.get('steamos') else '')
                    + "</p>")
    elif dk.get('steamos'):
        lines.append((f"SteamOS {dk['steamos']}", None, False))
        more.append(f"<p><b>SteamOS</b>: {html.escape(dk['steamos'])}</p>")
    ac = tips.get('anticheat') or {}
    if ac.get('status'):
        bad = ac['status'] in ('Broken', 'Denied')
        lines.append((f"Anti-cheat {ac['status'].lower()}", P['danger'] if bad else None, bad))
        link = (f" <a href='{html.escape(ac['url'])}' style='color:{P['accent']}'>details</a>"
                if str(ac.get('url', '')).startswith('https://') else '')
        more.append(f"<p><b>Anti-cheat</b> ({html.escape(', '.join(ac.get('anticheats') or []))})"
                    f": {html.escape(ac['status'])}"
                    + (f" — {html.escape(ac['note'])}" if ac.get('note') else '') + link + "</p>")
    pw = tips.get('pcgw') or {}
    if pw:
        sup = pw.get('support') or {}
        good = [k for k, v in sup.items() if v in ('true', 'always on')]
        if good:
            lines.append((', '.join(good[:3]), P['subtext'], False))
        chips = " · ".join(f"<span style='color:{P[PCGW_COLORS.get(v, 'muted')]}'>"
                           f"{html.escape(k)}: {html.escape(PCGW_WORDS.get(v, v))}</span>"
                           for k, v in sup.items())
        fixes = pw.get('fixes') or []
        link = (f"<a href='{html.escape(pw['url'])}' style='color:{P['accent']}'>"
                "Open on PCGamingWiki</a>" if str(pw.get('url', '')).startswith('https://')
                else '')
        more.append("<p>" + (chips or "No support table yet") + "</p>"
                    + (f"<p>{html.escape(', '.join(pw.get('apis') or []))}</p>"
                       if pw.get('apis') else '')
                    + ("<p><b>Essential fixes</b>: " + html.escape('; '.join(fixes)) + "</p>"
                       if fixes else '')
                    + (f"<p>{link}</p>" if link else ''))
        if fixes:
            lines.append((f"{len(fixes)} essential fix{'es' if len(fixes) != 1 else ''}",
                          P['warning'], False))
    return lines, "".join(more)


# --------------------------------------------------------------------------
# Game menu — the one place every per-game action lives (⋮ and right-click)
# --------------------------------------------------------------------------

def is_installed(g: dict) -> bool:
    return g.get('installed', True) is not False


def install_fraction(g: dict) -> float | None:
    """How far Steam is with a download (None: unknown or not downloading)."""
    i = g.get('installing') or {}
    return min(1.0, i['done'] / i['total']) if i.get('total') else None


def play_label(g: dict) -> str:
    """What the Play button says: the truth about the game right now."""
    if g.get('incomplete'):
        return "Resume install"
    if not is_installed(g):
        if g.get('installing'):
            if g['installing'].get('paused'):
                return "Download paused"
            f = install_fraction(g)
            return f"Installing {int(f * 100)}%" if f is not None else "Installing…"
        if g.get('install_requested'):
            return "Waiting for " + ("Steam" if g.get('source') == 'steam' else "Heroic")
        return "Install"
    if g.get('playing'):
        return "Playing"
    if g.get('launching'):
        return "Starting…"
    if g.get('busy'):
        return "Working…"
    return "Play"


def play_enabled(g: dict) -> bool:
    if g.get('incomplete'):
        return not g.get('resuming')
    if not is_installed(g):
        return bool(g.get('install_url')) and not g.get('installing') \
            and not g.get('install_requested')
    return bool(g.get('launcher')) and not g.get('playing') and not g.get('launching') \
        and not g.get('busy') and not g.get('stale')


def can_quick_play(g: dict) -> bool:
    """Whether a double click / A may start the game without its page:
    installed, finished and free to start (never an install or a resume)."""
    return is_installed(g) and not g.get('incomplete') and play_enabled(g)
