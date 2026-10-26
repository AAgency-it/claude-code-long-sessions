#!/usr/bin/env python3
"""Where did the time of your long Claude Code run go?

Reads the session transcripts (JSONL) that Claude Code keeps under ~/.claude/projects and answers, for each
main session and for the whole run (one session, or several working in parallel):

  1. How long was the run, and how much of it was the model really working (active hours)?
  2. How long were the pauses, where did the long ones fall?
  3. How often did the session compact, at what size, how big was the context after it, how long did it take?
  4. How many tool calls failed, why, and how many of those failures were avoidable?
  5. How many messages did the sessions send to each other?

It is the companion of analyze_usage.py: that one looks at tokens, this one looks at time and failures.
There is no money in this report, only durations and counts. The transcripts are only read, and the
conversations are never printed: the only text shown is the first line of each tool error, with paths and
numbers replaced.

Main sessions and subagents are separated: a subagent lives in <project>/<session id>/subagents/agent-*.jsonl.
Subagents are only summed up (how many, how many turns, how many active hours) and never enter the totals
of the main sessions.

Usage:
  python tools/run_report.py [--projects-dir DIR] [--since DATE] [--until DATE] [--session ID ...]
                             [--active-gap 300] [--gap-report 3600] [--out report.json] [--json]

Only the standard library is needed (Python 3.10 or newer).
"""
import argparse
import collections
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_ACTIVE_GAP = 300        # seconds: a gap up to this long between two records still counts as work
DEFAULT_GAP_REPORT = 3600       # seconds: a gap at least this long is listed
TOP_LINES = 10                  # how many error lines are shown
LINE_WIDTH = 90                 # characters of an error line

# The categories of tool errors. The first rule that matches wins, so the order matters.
# The first three are avoidable: the model had what it needed to get them right.
EDIT_BEFORE_READ = 'edit before read'
FILE_CHANGED = 'file changed since read'
HOOK = 'blocked by a hook'
COMMAND = 'command exited with an error'
NOT_FOUND = 'string to replace not found'
DENIED = 'denied by permissions'
BROWSER = 'browser'
OTHER = 'other'
CATEGORIES = (EDIT_BEFORE_READ, FILE_CHANGED, HOOK, COMMAND, NOT_FOUND, DENIED, BROWSER, OTHER)
AVOIDABLE = {EDIT_BEFORE_READ, FILE_CHANGED, HOOK}


# --- small helpers ---

def parse_ts(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def text_of(content):
    """The text of a tool result (a string, or a list of blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            parts.append(block.get('text', ''))
        elif isinstance(block, str):
            parts.append(block)
    return '\n'.join(parts)


def median(values):
    return statistics.median(values) if values else None


# --- tool errors ---

# A tag such as <tool_use_error>: Claude Code wraps the text of many tool errors in it.
TAG = re.compile(r'</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>')


def strip_tags(text):
    return TAG.sub('', text or '')


def classify_error(text, tool):
    """The category of a failed tool call, from the text of the error and the name of the tool."""
    s = strip_tags(text).lstrip()
    low = s.lower()
    if s.startswith('File has not been read yet'):
        return EDIT_BEFORE_READ
    if s.startswith('File has been modified since read'):
        return FILE_CHANGED
    if 'hook error' in low or s.startswith('PreToolUse:'):
        return HOOK
    if s.startswith('Exit code'):
        return COMMAND
    if s.startswith('String to replace not found'):
        return NOT_FOUND
    if 'denied' in low or "doesn't want" in low:
        return DENIED
    name = (tool or '').lower()
    if 'chrome' in name or 'browser' in name:
        return BROWSER
    return OTHER


# A path: a drive letter, or something that starts with / ~/ ./ ../ and does not follow a word ("and/or").
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|(?<![\w.~])(?:\.{1,2}/|~/|/))[^\s'\"`,;)]*")


def normalise_line(text):
    """The first non-empty line of an error, with paths as <path> and numbers as N, at most 90 characters."""
    line = ''
    for candidate in strip_tags(text).splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    line = PATH.sub('<path>', line)
    line = re.sub(r'\d+', 'N', line)
    return line[:LINE_WIDTH]


def errors_summary(errors, tool_results):
    """Totals, shares, categories, tools and the most common lines of a list of tool errors."""
    by_category = collections.Counter(e['category'] for e in errors)
    by_tool = collections.Counter(e['tool'] for e in errors)
    lines = collections.Counter(e['line'] for e in errors)
    avoidable = sum(1 for e in errors if e['category'] in AVOIDABLE)
    return dict(
        total=len(errors), tool_results=tool_results,
        share_of_results=len(errors) / tool_results if tool_results else 0.0,
        avoidable=avoidable, avoidable_share=avoidable / len(errors) if errors else 0.0,
        by_category={c: by_category[c] for c in CATEGORIES if by_category[c]},
        by_tool=dict(by_tool.most_common()),
        top_lines=[dict(line=line, count=n) for line, n in lines.most_common(TOP_LINES)])


# --- finding the transcripts ---

def discover_sessions(projects_dir, session_ids=None):
    """Main sessions under projects_dir (a projects folder, or one project folder), with their subagents."""
    root = os.path.expanduser(projects_dir)
    if not os.path.isdir(root):
        return []
    files = glob.glob(os.path.join(root, '*.jsonl')) + glob.glob(os.path.join(root, '*', '*.jsonl'))
    sessions = []
    for path in sorted(set(files)):
        sid = os.path.splitext(os.path.basename(path))[0]
        if session_ids and sid not in session_ids:
            continue
        subs = sorted(glob.glob(os.path.join(os.path.dirname(path), sid, 'subagents', 'agent-*.jsonl')))
        sessions.append(dict(id=sid, path=path, subagents=subs))
    return sessions


# --- reading one transcript ---

def context_of(usage):
    """Tokens in the context of a model call: new input, cache reads and cache writes."""
    created = usage.get('cache_creation_input_tokens')
    if created is None:
        created = sum(v for v in (usage.get('cache_creation') or {}).values() if isinstance(v, int))
    return (usage.get('input_tokens') or 0) + (usage.get('cache_read_input_tokens') or 0) + (created or 0)


def analyze_transcript(path, since=None, until=None):
    """Parse one JSONL transcript into the raw facts the report needs (no conversation text is kept).

    `since` and `until` are datetimes: the records outside them are ignored, as if they were not there.
    """
    lo = since.timestamp() if since else None
    hi = until.timestamp() if until else None
    stamps, turn_ids, anonymous_turns = [], set(), 0
    tool_name, send_ids = {}, set()
    tool_results, errors, compactions = 0, [], []
    waiting = None                       # the compaction that still waits for the first reply after it
    context_by_id = {}
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            stamp = o.get('timestamp')
            if not isinstance(stamp, str):
                continue
            try:
                t = parse_ts(stamp).timestamp()
            except ValueError:
                continue
            if (lo is not None and t < lo) or (hi is not None and t > hi):
                continue
            stamps.append(t)
            kind = o.get('type')
            if kind == 'system':
                if o.get('subtype') == 'compact_boundary':
                    meta = o.get('compactMetadata') or {}
                    compactions.append(dict(ts=t, trigger=meta.get('trigger'), pre=meta.get('preTokens'),
                                            post=meta.get('postTokens'), duration_ms=meta.get('durationMs'), restart_id=None, restart=None))
                    waiting = compactions[-1]
                continue
            if kind == 'assistant':
                msg = o.get('message') or {}
                if msg.get('model') == '<synthetic>':
                    continue
                mid = msg.get('id')
                ctx = context_of(msg.get('usage') or {})
                if mid is None:
                    anonymous_turns += 1
                else:
                    turn_ids.add(mid)
                    context_by_id[mid] = ctx          # a message streamed on several lines: the last one wins
                if waiting is not None:
                    waiting['restart_id'], waiting['restart'] = mid, ctx
                    waiting = None
                for b in msg.get('content') or []:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        tool_name[b.get('id')] = b.get('name') or '?'
                        if b.get('name') == 'SendMessage':
                            send_ids.add(b.get('id'))
                continue
            if kind == 'user':
                content = (o.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue
                for b in content:
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        tool_results += 1
                        if b.get('is_error') is True:
                            text = text_of(b.get('content'))
                            tool = tool_name.get(b.get('tool_use_id'), '?')
                            errors.append(dict(category=classify_error(text, tool), tool=tool,
                                               line=normalise_line(text)))
    for c in compactions:
        if c['restart_id'] is not None:
            c['restart'] = context_by_id.get(c['restart_id'], c['restart'])
    return dict(stamps=stamps, turns=len(turn_ids) + anonymous_turns, compactions=compactions,
                tool_results=tool_results, errors=errors, send_messages=len(send_ids))


# --- the numbers ---

def active_and_gaps(stamps, active_gap, gap_report):
    """(active seconds, long gaps) of a list of timestamps (epoch seconds).

    Active time is the sum of the intervals between two consecutive records that are at most `active_gap`
    seconds long. A gap of at least `gap_report` seconds is listed with its start, end and minutes.
    """
    ordered = sorted(stamps)
    active, gaps = 0.0, []
    for a, b in zip(ordered, ordered[1:]):
        d = b - a
        if d <= active_gap:
            active += d
        if d >= gap_report:
            gaps.append(dict(start=iso(a), end=iso(b), minutes=d / 60))
    return active, gaps


def compaction_summary(compactions, turns):
    """How many compactions, at what size, with what context after them, and how long they took."""
    number = (int, float)
    pre = [c['pre'] for c in compactions if isinstance(c['pre'], number)]
    summary = [c['post'] for c in compactions if isinstance(c['post'], number)]
    restart = [c['restart'] for c in compactions if c['restart'] is not None]
    room = [c['pre'] - c['restart'] for c in compactions if isinstance(c['pre'], number) and c['restart'] is not None]
    ms = [c['duration_ms'] for c in compactions if isinstance(c['duration_ms'], number)]
    n = len(compactions)
    return dict(count=n, median_pre_tokens=median(pre), summary_median=median(summary),
                restart_contexts=restart, restart_median=median(restart), room_median=median(room),
                mean_minutes=sum(ms) / len(ms) / 60000 if ms else None, total_minutes=sum(ms) / 60000,
                turns_per_compaction=turns / n if n else None)


def summarize(raw, active_gap, gap_report):
    """The numbers of one main session from its raw facts."""
    stamps = raw['stamps']
    active, gaps = active_and_gaps(stamps, active_gap, gap_report)
    first, last = min(stamps), max(stamps)
    return dict(first=iso(first), last=iso(last), hours=(last - first) / 3600, active_hours=active / 3600,
                turns=raw['turns'], seconds_per_turn=active / raw['turns'] if raw['turns'] else None,
                compactions=compaction_summary(raw['compactions'], raw['turns']),
                tool_errors=errors_summary(raw['errors'], raw['tool_results']),
                send_messages=raw['send_messages'], gaps=gaps)


def build_report(sessions, since, until, active_gap, gap_report):
    """Read every selected session and its subagents and build the report (a dict, ready for JSON)."""
    report_sessions, raws = {}, []
    sub_count = sub_turns = 0
    sub_active = 0.0
    for s in sessions:
        raw = analyze_transcript(s['path'], since, until)
        if not raw['stamps']:
            continue                                    # nothing of this session in the selected time range
        subs = [r for r in (analyze_transcript(p, since, until) for p in s['subagents']) if r['stamps']]
        sub = dict(count=len(subs), turns=sum(r['turns'] for r in subs),
                   active_hours=sum(active_and_gaps(r['stamps'], active_gap, gap_report)[0] for r in subs) / 3600)
        sub_count += sub['count']
        sub_turns += sub['turns']
        sub_active += sub['active_hours']
        raws.append(raw)
        report_sessions[s['id']] = dict(summarize(raw, active_gap, gap_report), subagents=sub)
    totals = dict(sessions=len(raws))
    if raws:
        stamps = [t for r in raws for t in r['stamps']]
        run_active, _ = active_and_gaps(stamps, active_gap, gap_report)
        turns = sum(r['turns'] for r in raws)
        sessions_active = sum(x['active_hours'] for x in report_sessions.values())
        totals.update(
            window=dict(first=iso(min(stamps)), last=iso(max(stamps)), hours=(max(stamps) - min(stamps)) / 3600),
            sessions_hours=sum(x['hours'] for x in report_sessions.values()),
            sessions_active_hours=sessions_active, active_hours=run_active / 3600, turns=turns,
            seconds_per_turn=sessions_active * 3600 / turns if turns else None,
            compactions=compaction_summary([c for r in raws for c in r['compactions']], turns),
            tool_errors=errors_summary([e for r in raws for e in r['errors']], sum(r['tool_results'] for r in raws)),
            send_messages=sum(r['send_messages'] for r in raws))
    totals['subagents'] = dict(count=sub_count, turns=sub_turns, active_hours=sub_active)
    return dict(filters=dict(since=since.isoformat() if since else None, until=until.isoformat() if until else None),
                options=dict(active_gap_s=active_gap, gap_report_s=gap_report),
                sessions=report_sessions, totals=totals)


# --- printing ---

def kilo(tokens):
    return '-' if tokens is None else f'{int(tokens / 1000 + 0.5)}k'


def number(x, digits=1):
    return '-' if x is None else f'{x:.{digits}f}'


def pct(x):
    return f'{100 * x:.0f}%'


def row(name, hours, active, turns, spt, c, e):
    return (f"{name[:38]:<38} {hours:>6.1f} {active:>8.1f} {turns:>6} {number(spt, 0):>7} {c['count']:>11} "
            f"{kilo(c['median_pre_tokens']):>10} {kilo(c['summary_median']):>8} {kilo(c['restart_median']):>10} "
            f"{kilo(c['room_median']):>8} {c['total_minutes']:>14.1f} {e['total']:>11} {e['avoidable']:>9}")


def short(stamp):
    """An ISO timestamp as 2026-01-31T08:00Z (the transcripts are in UTC)."""
    return stamp[:16] + 'Z'


def print_report(rep, out=None):
    out = out or sys.stdout
    w = lambda s='': print(s, file=out)
    t = rep['totals']
    w('Times and counts only; all times are in UTC. Active h = time between records that are at most '
      f"{rep['options']['active_gap_s']} s apart.")
    w()
    w(f"{'session':<38} {'hours':>6} {'active h':>8} {'turns':>6} {'s/turn':>7} {'compactions':>11} "
      f"{'compact at':>10} {'summary':>8} {'restart at':>10} {'room':>8} {'compaction min':>14} "
      f"{'tool errors':>11} {'avoidable':>9}")
    for sid, s in rep['sessions'].items():
        w(row(sid, s['hours'], s['active_hours'], s['turns'], s['seconds_per_turn'], s['compactions'], s['tool_errors']))
    w(row('TOTAL', t['sessions_hours'], t['sessions_active_hours'], t['turns'], t['seconds_per_turn'],
          t['compactions'], t['tool_errors']))
    w()
    w('"compact at" is the median context size when it compacts. "summary" is the median size of the summary it')
    w('writes. "restart at" is the median context of the first reply after it: the summary plus what Claude Code')
    w('loads again (files read, skills, tools). "room" is the median of (compact at - restart at): the working')
    w('space between a restart and the next compaction. TOTAL hours are the sum over the sessions.')
    win = t['window']
    w(f"The run: {win['hours']:.1f} h from {short(win['first'])} to {short(win['last'])}, "
      f"{t['active_hours']:.1f} h with at least one session working.")
    c = t['compactions']
    parts = []
    if t['seconds_per_turn']:
        parts.append(f"one turn every {t['seconds_per_turn']:.0f} s of work")
    if c['turns_per_compaction']:
        parts.append(f"one compaction every {c['turns_per_compaction']:.0f} turns")
    parts.append(f"{t['send_messages']} SendMessage calls (messages between sessions)")
    w('That is ' + ', '.join(parts) + '.')

    e = t['tool_errors']
    w()
    w(f"Tool errors: {e['total']} of {e['tool_results']} tool results ({pct(e['share_of_results'])}). "
      f"Avoidable: {e['avoidable']} ({pct(e['avoidable_share'])} of the errors).")
    for category, n in e['by_category'].items():
        mark = '  avoidable' if category in AVOIDABLE else ''
        w(f'  {category:<30} {n:>6}{mark}')
    if e['by_tool']:
        w('  by tool: ' + ', '.join(f'{name} {n}' for name, n in list(e['by_tool'].items())[:5]))
    if e['top_lines']:
        w('  most common first lines (paths and numbers replaced):')
        for x in e['top_lines']:
            w(f"    {x['count']:>4}  {x['line']}")

    gaps = [(sid, g) for sid, s in rep['sessions'].items() for g in s['gaps']]
    w()
    limit = rep['options']['gap_report_s'] / 60
    if gaps:
        w(f'Gaps of {limit:.0f} min or more ({len(gaps)}, longest first):')
        for sid, g in sorted(gaps, key=lambda x: -x[1]['minutes'])[:20]:
            w(f"  {sid[:38]:<38} {short(g['start'])} -> {short(g['end'])}{g['minutes']:>7.0f} min")
        if len(gaps) > 20:
            w(f'  ... and {len(gaps) - 20} more (all of them are in the JSON written with --out)')
    else:
        w(f'Gaps of {limit:.0f} min or more: none.')

    sub = t['subagents']
    w()
    w(f"Subagents (not in the numbers above): {sub['count']} transcripts, {sub['turns']} turns, "
      f"{sub['active_hours']:.1f} active hours.")


# --- command line ---

def when_arg(end_of_day):
    def parse(s):
        try:
            d = parse_ts(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f'not a date: {s!r} (use 2026-01-31 or 2026-01-31T08:00:00Z)')
        if end_of_day and len(s) <= 10:
            d += timedelta(days=1)                      # a plain date as --until includes the whole day
        return d
    return parse


def build_parser():
    ap = argparse.ArgumentParser(description='Where did the time of a long Claude Code run go? (times and counts, no money)')
    ap.add_argument('--projects-dir', default=os.path.join('~', '.claude', 'projects'),
                    help='Claude Code projects folder, or one project folder (default ~/.claude/projects)')
    ap.add_argument('--since', type=when_arg(False), help='only records from this date/time on (UTC if no zone)')
    ap.add_argument('--until', type=when_arg(True), help='only records up to this date/time (a plain date includes the day)')
    ap.add_argument('--session', action='append', help='session id (the file name without .jsonl); repeatable')
    ap.add_argument('--active-gap', type=int, default=DEFAULT_ACTIVE_GAP,
                    help=f'longest gap in seconds that still counts as work (default {DEFAULT_ACTIVE_GAP})')
    ap.add_argument('--gap-report', type=int, default=DEFAULT_GAP_REPORT,
                    help=f'list the gaps of at least this many seconds (default {DEFAULT_GAP_REPORT})')
    ap.add_argument('--out', help='write the full report as JSON to this file')
    ap.add_argument('--json', action='store_true', help='print only the JSON report on the standard output')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    sessions = discover_sessions(args.projects_dir, set(args.session) if args.session else None)
    if not sessions:
        print(f'No sessions found under {args.projects_dir}')
        return 1
    rep = build_report(sessions, args.since, args.until, args.active_gap, args.gap_report)
    if not rep['sessions']:
        print('No records found in the selected sessions and time range.')
        return 1
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    print_report(rep)
    if args.out:
        print(f'\nFull report written to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
