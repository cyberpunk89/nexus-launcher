"""A draft plus the user's choices -> the exact issue text. Pure: no files,
no clock, no machine reads, so the preview and what is posted can't drift
(a relay can vendor this file as it is).

    render(draft, selection=None) -> {title, body, lines, groups, length}
    issue_url(rendered, draft) -> {url, clipboard}

selection (every key optional):
    exclude      group keys left out; None = the draft's default_exclude
                 (the Proton log tail is off until asked for)
    remove       line numbers to replace with [line removed by reporter]
    with_game    include the Game identity group (the only way a game's
                 title gets into a report)
    description  the user's own words, shown first

Line numbers count the data lines of the full report, every group on
(Game identity included), so leaving a group out never renumbers the rest.
Every group is a fenced text block inside <details>: nothing in a report is
read as markdown, links aren't clickable and mentions don't notify."""

from urllib.parse import urlencode

ORDER = ('game', 'error', 'app', 'system', 'graphics', 'runtime', 'settings', 'log',
         'launch', 'proton_log')
LABELS = {'game': 'Game identity', 'error': 'Error', 'app': 'App', 'system': 'System',
          'graphics': 'Graphics', 'runtime': 'Runtime', 'settings': 'Settings',
          'log': 'Recent log', 'launch': 'Game run', 'proton_log': 'Proton log tail'}
ALWAYS = ('app',)                       # without it a report is useless
REMOVED = '[line removed by reporter]'
MAX_URL = 7500                          # VS Code's cap for an issues/new URL
MAX_BODY = 60_000                       # GitHub refuses past 65,536
CLIPBOARD_NOTE = ("The report is too long for a link. Nexus copied it to your clipboard: "
                  "paste it here.")
DESCRIPTION_MAX = 2000


def _fence_safe(line: str) -> str:
    return str(line).replace('```', "'''")


def numbered(draft: dict) -> list[dict]:
    """[{n, group, text}] for the full report: every group, in ORDER."""
    out, n = [], 0
    groups = draft.get('groups') or {}
    for key in ORDER:
        for text in groups.get(key) or ():
            n += 1
            out.append({'n': n, 'group': key, 'text': str(text)})
    return out


def included(draft: dict, selection: dict | None = None) -> list[str]:
    sel = selection or {}
    exclude = sel.get('exclude')
    if exclude is None:
        exclude = draft.get('default_exclude') or ()
    exclude = set(exclude) - set(ALWAYS)
    keys = [k for k in ORDER if (draft.get('groups') or {}).get(k) and k not in exclude]
    return [k for k in keys if k != 'game' or sel.get('with_game')]


def render(draft: dict, selection: dict | None = None) -> dict:
    sel = selection or {}
    removed = {int(n) for n in sel.get('remove') or ()}
    keys = included(draft, sel)
    lines = numbered(draft)
    by_group: dict[str, list[str]] = {}
    for ln in lines:
        ln['included'] = ln['group'] in keys
        ln['removed'] = ln['n'] in removed
        by_group.setdefault(ln['group'], []).append(REMOVED if ln['removed'] else ln['text'])
    edition = draft.get('edition') or '?'
    body = [f"Nexus {draft.get('version') or '?'} ({edition}), "
            f"{draft.get('kind') or 'user-report'}"
            + (f", sig:{draft['signature']}" if draft.get('signature') else '')]
    desc = str(sel.get('description') or '').strip()[:DESCRIPTION_MAX]
    if desc:
        body += ['', '### What happened', '', '```text', *map(_fence_safe, desc.splitlines()),
                 '```']
    for key in keys:
        body += ['', f'<details><summary>{LABELS[key]}</summary>', '', '```text',
                 *map(_fence_safe, by_group.get(key, [])), '```', '', '</details>']
    body += ['', '<sub>Made by Nexus\'s "Report a problem". Names and paths were replaced '
                 'before it left the computer.</sub>']
    text = '\n'.join(body)
    if len(text) > MAX_BODY:
        text = text[:MAX_BODY - 40].rsplit('\n', 1)[0] + '\n```\n\n(cut: too long)\n'
    groups = [{'key': k, 'label': LABELS[k], 'lines': len((draft.get('groups') or {}).get(k)
                                                             or ()),
               'included': k in keys,
               'default': k not in (draft.get('default_exclude') or ()) and k != 'game'}
              for k in ORDER if (draft.get('groups') or {}).get(k)]
    return {'title': draft.get('title') or 'Problem report', 'body': text, 'lines': lines,
            'groups': groups, 'length': len(text)}


def issue_url(rendered: dict, draft: dict, repo: str | None = None) -> dict:
    """{url, clipboard}: the prefilled issues/new link for the bug form.
    Past MAX_URL characters the report field carries CLIPBOARD_NOTE, and
    `clipboard` is the body to copy; otherwise clipboard is None."""
    if repo is None:
        from nexus.updates import REPO as repo
    base = f"https://github.com/{repo}/issues/new"
    fields = {'template': 'bug.yml', 'title': rendered['title'],
              'version': str(draft.get('version') or ''),
              'edition': str(draft.get('edition') or '')}
    url = f"{base}?{urlencode({**fields, 'report': rendered['body']})}"
    if len(url) <= MAX_URL:
        return {'url': url, 'clipboard': None}
    return {'url': f"{base}?{urlencode({**fields, 'report': CLIPBOARD_NOTE})}",
            'clipboard': rendered['body']}
