"""Bug reports from the command line (the window's "Report a problem" page
drives the same flags):

    --report-pending                 crash files waiting to be reported
    --report-collect [--crash ID] [--game SLUG] [--log-file FILE]
                                     gather a scrubbed draft (slow: Doctor)
    --report-preview DRAFT           the exact issue text
    --report-url DRAFT               the prefilled GitHub issue link
    --report-dismiss ID              forget one crash file

Preview and url take --exclude GROUP[,GROUP], --remove-line N (repeat),
--with-game and --describe TEXT. Every one supports --json. Ids are checked
against their pattern and never used as a path of their own."""

import json
import sys
from pathlib import Path

from nexus.report import crash as C, render as RD, scrub as S, sysinfo


def _fail(as_json: bool, kind: str, message: str) -> None:
    if as_json:
        print(json.dumps({'error': {'kind': kind, 'message': message}}))
    else:
        print(message, file=sys.stderr)
    sys.exit(1)


def _selection(args, draft: dict) -> dict:
    exclude = None
    if args.report_exclude is not None:
        exclude = [g.strip() for item in args.report_exclude for g in item.split(',')
                   if g.strip()]
    remove = [int(n) for item in args.report_remove_line or ()
              for n in str(item).split(',') if n.strip().isdigit()]
    desc = args.report_describe
    if desc:
        desc = S.scrub(desc, S.context()) if not args.report_with_game else desc
    return {'exclude': exclude, 'remove': remove, 'with_game': bool(args.report_with_game),
            'description': desc or ''}


def _draft(draft_id: str, as_json: bool) -> dict:
    if not C.DRAFT_ID.match(draft_id or ''):
        _fail(as_json, 'bad-id', f"{draft_id!r} isn't a report draft id.")
    draft = C.load_draft(draft_id)
    if not draft:
        _fail(as_json, 'not-found', f"No report draft {draft_id}.")
    return draft


def cmd_report_pending(as_json: bool) -> None:
    items = C.pending()
    if as_json:
        print(json.dumps({'reports': items}))
        return
    if not items:
        print("No crash reports waiting.")
    for r in items:
        print(f"{r['id']}  {r['kind']}  {r['type']}  sig:{r['signature']}  "
              f"x{r['count']}  {r['last']}")


def cmd_report_collect(args) -> None:
    as_json = args.json
    crash = None
    if args.report_crash:
        if not C.CRASH_ID.match(args.report_crash):
            _fail(as_json, 'bad-id', f"{args.report_crash!r} isn't a crash id.")
        crash = C.load(args.report_crash)
        if not crash:
            _fail(as_json, 'not-found', f"No crash report {args.report_crash}.")
    game = None
    if args.report_game:
        entry = registry_get(args.report_game)
        if not entry:
            _fail(as_json, 'not-found', f"No game {args.report_game} in the library.")
        game = {**entry, 'slug': args.report_game}
    log_lines = None
    if args.report_log_file:
        log_lines = sysinfo.tail(Path(args.report_log_file).expanduser(), C.LOG_LINES)
    draft = sysinfo.collect(crash=crash, log_lines=log_lines, game=game)
    did = C.save_draft(draft)
    if not did:
        _fail(as_json, 'io', "The report draft couldn't be saved.")
    if as_json:
        print(json.dumps({'draft': did, 'title': draft['title'], 'kind': draft['kind'],
                          'groups': list(draft['groups']),
                          'default_exclude': draft['default_exclude']}))
    else:
        print(f"Report draft {did}: {draft['title']}")
        print(f"See it with: nexus-cli --report-preview {did}")


def cmd_report_preview(args) -> None:
    draft = _draft(args.report_preview, args.json)
    out = RD.render(draft, _selection(args, draft))
    if args.json:
        print(json.dumps({'draft': draft['id'], **out}))
    else:
        print(out['title'])
        print()
        print(out['body'])


def cmd_report_url(args) -> None:
    draft = _draft(args.report_url, args.json)
    out = RD.render(draft, _selection(args, draft))
    link = RD.issue_url(out, draft)
    if args.json:
        print(json.dumps({'draft': draft['id'], 'title': out['title'], **link}))
        return
    print(link['url'])
    if link['clipboard']:
        print("\nToo long for a link: paste this into the report field:\n", file=sys.stderr)
        print(link['clipboard'])


def cmd_report_dismiss(crash_id: str, as_json: bool) -> None:
    if not C.CRASH_ID.match(crash_id or ''):
        _fail(as_json, 'bad-id', f"{crash_id!r} isn't a crash id.")
    ok = C.dismiss(crash_id)
    if as_json:
        print(json.dumps({'dismissed': ok}))
    elif not ok:
        print(f"No crash report {crash_id}.", file=sys.stderr)
    if not ok:
        sys.exit(1)


# Siblings, imported last: they import this module too, and every use
# below is inside a function, so the cycle resolves at call time.
from .registry import registry_get  # noqa: E402
