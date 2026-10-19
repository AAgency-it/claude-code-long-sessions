#!/usr/bin/env python3
"""Where do the tokens of your long Claude Code sessions go?

Reads the session transcripts (JSONL) that Claude Code keeps under ~/.claude/projects, deduplicates the
model calls by message.id, prices them with a table of list prices (as weights: a subscription does not
pay per token, but the ratios tell you what is expensive), and answers four questions:

  1. How much of the cost is re-reading the cached context?
  2. What started each turn: a person, a /goal re-run, a message from another session, a notification?
  3. Who put the new tokens into the context (tool results, your messages, a cache rewrite)? This one is
     only in the JSON written with --out.
  4. What would it have cost with another compaction threshold, a cap on /goal re-runs, or a 1-hour
     cache for the subagents?

Main sessions and subagents are analysed separately: a subagent lives in
<project>/<session id>/subagents/agent-*.jsonl and has its own context and cache.

Usage:
  python tools/analyze_usage.py [--projects-dir DIR] [--since DATE] [--until DATE]
                                [--session ID ...] [--out report.json]
                                [--thresholds 500000,400000,300000,200000] [--rerun-cap 3]
                                [--combined-threshold 400000] [--main-cache-ttl 1h]
                                [--subagent-cache-ttl 5m]

Only the standard library is needed (Python 3.10 or newer).
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# Prices in USD per million tokens, used as weights.
# These are list prices at time of writing, check current pricing and edit the table to match.
#   inp = uncached input, w5 = cache write (5-minute TTL), w1 = cache write (1-hour TTL),
#   rd = cache read, out = output.
PRICES = {
    'opus':   dict(inp=4.0, w5=5.0, w1=8.0, rd=0.20, out=20.0),
    'sonnet': dict(inp=2.0, w5=2.5, w1=4.0, rd=0.20, out=10.0),
    'haiku':  dict(inp=0.10, w5=0.125, w1=0.20, rd=0.01, out=0.50),
}
DEFAULT_FAMILY = 'opus'             # a model whose name matches no family is priced as this one
CHARS_PER_TOKEN = 3.3               # to turn the size of a tool result into tokens
IMAGE_TOKENS = 1500                 # one image in a message or tool result
POST_COMPACTION_CONTEXT = 45_000    # tokens in the context right after a compaction (summary, prompt, tools)
SUMMARY_OUTPUT_TOKENS = 12_000      # tokens the model writes for a compaction summary
NO_COMPACTION = 10**7               # a threshold nobody reaches: "compaction off" in the simulation
DEFAULT_RERUN_CAP = 3               # the cap on idle /goal re-runs we recommend (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP)

RERUN = 'goal: re-run after stop'   # the trigger label of a /goal re-run (the "Stop hook feedback" turns)
LABEL_BASE = 'base: segment start (prompt, tools, summary)'
LABEL_REWRITE = 'cache rewritten (expired or invalidated)'
LABEL_RESPONSE = 'model response (text, thinking, calls)'
LABEL_UNATTRIBUTED = 'not attributed'
LABEL_REMINDERS = 'reminders and skills'

TOKEN_KEYS = ('inp', 'w5', 'w1', 'rd', 'out')


# --- small helpers ---

def family(model):
    model = model or ''
    for name in PRICES:
        if name in model:
            return name
    return DEFAULT_FAMILY


def parse_ts(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def seconds_between(a, b):
    """Seconds from timestamp a to timestamp b (0 when one of them is missing)."""
    if not a or not b:
        return 0.0
    try:
        return (parse_ts(b) - parse_ts(a)).total_seconds()
    except ValueError:
        return 0.0


def cost_of(u, fam):
    p = PRICES[fam]
    return {k: u[k] * p[k] / 1e6 for k in TOKEN_KEYS}


def text_of(content):
    """(text, number of images) of a message content, a tool result included."""
    if isinstance(content, str):
        return content, 0
    parts, images = [], 0
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind == 'text':
            parts.append(block.get('text', ''))
        elif kind == 'image':
            images += 1
        elif kind == 'tool_result':
            t, i = text_of(block.get('content'))
            parts.append(t)
            images += i
    return '\n'.join(parts), images


# --- what started a turn ---
# The checks come BEFORE the isMeta flag: the harness marks goal re-runs, messages from other sessions and
# notifications as "meta", and a classifier that looked at isMeta first would never see them.

def classify_user(text, is_meta):
    """The trigger label of a user-side message, or None if it does not start a turn (reminders, skills...)."""
    s = text.lstrip()
    if s.startswith('Stop hook feedback'):
        return RERUN
    if 'Goal check-in' in s[:400]:
        return 'goal: check-in'
    if s.startswith('Another Claude session sent a message') or s.startswith('[Cross-session'):
        return 'message from another session'
    if s.startswith('<task-notification>'):
        if 'Monitor event' in s[:600]:
            return 'notification: monitor'
        if '<status>' in s and '<output-file>' in s and 'agent' in s[:600].lower():
            return 'notification: subagent finished'
        return 'notification: background work'
    if s.startswith('This session is being continued'):
        return 'resume after compaction'
    for prefix in ('[Usage limit', '[Earlier usage', 'Base directory for this skill', '<system-reminder>', '[Image', 'Caveat:'):
        if s.startswith(prefix):
            return None
    if is_meta:
        return None
    if s.startswith('<command-name>') or s.startswith('<local-command'):
        return 'person: command'
    if s.startswith('<scheduled') or 'ScheduleWakeup' in s[:200] or s.startswith('<<autonomous'):
        return 'scheduled wake-up'
    return 'person: message'


def trigger_group(label):
    if label.startswith('person:'):
        return 'person'
    if label.startswith('goal:'):
        return 'goal'
    if label.startswith('notification:'):
        return 'notification'
    if label == 'message from another session':
        return 'other session'
    return 'other'


def trigger_shares(counts):
    """Percent of the turn-starting events per group (person, goal, other session, notification, other)."""
    groups = collections.Counter()
    for label, n in counts.items():
        groups[trigger_group(label)] += n
    total = sum(groups.values())
    return {g: 100.0 * n / total for g, n in groups.most_common()} if total else {}


def trigger_cost_shares(call_lists):
    """Percent of the cost per trigger group, summed over the calls of the given transcripts.

    A turn runs from the message that started it to the next one; every call of the turn carries the
    trigger of its turn (see analyze_transcript), so the cost of all of them goes to that trigger.
    """
    cost = collections.Counter()
    for calls in call_lists:
        for c in calls:
            cost[trigger_group(c['trigger'])] += c['total']
    total = sum(cost.values())
    return {g: 100.0 * v / total for g, v in cost.most_common()} if total else {}


# --- reading one transcript ---

def analyze_transcript(path, since=None, until=None, seen=None):
    """Parse one JSONL transcript into calls, compactions, trigger counts and a token attribution.

    `seen` is a set of message ids shared by the transcripts of one report: a call already counted in an
    earlier file (for instance a history copied into a new transcript) is not counted again.
    """
    calls, by_id, tool_name = [], {}, {}
    compactions, usage_limit, cross_messages = [], [], []
    trigger_counts = collections.Counter()
    trigger, seg, pending, stop_hooks, turn = 'start', 0, [], 0, 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            kind = o.get('type')
            stamp = o.get('timestamp')
            if stamp and (since or until):
                try:
                    d = parse_ts(stamp)
                except ValueError:
                    continue
                if (since and d < since) or (until and d > until):
                    continue
            if kind == 'system':
                subtype = o.get('subtype')
                if subtype == 'compact_boundary':
                    meta = o.get('compactMetadata') or {}
                    compactions.append(dict(ts=stamp, trigger=meta.get('trigger'), pre=meta.get('preTokens'),
                                            post=meta.get('postTokens')))
                    seg += 1
                    pending = []
                elif subtype == 'stop_hook_summary':
                    stop_hooks += 1
                continue
            if kind == 'attachment':
                rendered = o.get('rendered') or []
                n = sum(len(x.get('content', '') if isinstance(x.get('content'), str) else json.dumps(x.get('content')))
                        for x in rendered if isinstance(x, dict))
                if n:
                    pending.append(('attachment: ' + (o.get('attachment') or {}).get('type', '?'), n))
                continue
            if kind == 'user':
                content = (o.get('message') or {}).get('content')
                if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                    for b in content:
                        if isinstance(b, dict) and b.get('type') == 'tool_result':
                            text, images = text_of(b.get('content'))
                            name = tool_name.get(b.get('tool_use_id'), '?')
                            pending.append(('tool: ' + name, len(text) + images * IMAGE_TOKENS * CHARS_PER_TOKEN))
                    continue
                text, images = text_of(content)
                if '[Usage limit reached' in text[:200]:
                    usage_limit.append(stamp)
                label = classify_user(text, o.get('isMeta'))
                pending.append(('text: ' + (label or LABEL_REMINDERS), len(text) + images * IMAGE_TOKENS * CHARS_PER_TOKEN))
                if label == 'message from another session':
                    sender = re.search(r'from-name="([^"]*)"', text)
                    body = re.search(r'<cross-session-message[^>]*>(.*?)(?:</cross-session-message>|$)', text, re.S)
                    body = body.group(1) if body else text
                    cross_messages.append({'ts': stamp, 'from': sender.group(1) if sender else '?', 'chars': len(body.strip())})
                if label:
                    trigger = label
                    turn += 1
                    trigger_counts[label] += 1
                continue
            if kind == 'assistant':
                msg = o.get('message') or {}
                if msg.get('model') == '<synthetic>':
                    continue
                n_tools = 0
                for b in msg.get('content') or []:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        tool_name[b.get('id')] = b.get('name')
                        n_tools += 1
                u = msg.get('usage') or {}
                cc = u.get('cache_creation') or {}
                rec = dict(
                    ts=stamp, model=msg.get('model'), effort=o.get('effort'), seg=seg, trigger=trigger,
                    inp=u.get('input_tokens', 0) or 0,
                    w5=cc.get('ephemeral_5m_input_tokens', 0) or 0,
                    w1=cc.get('ephemeral_1h_input_tokens', 0) or 0,
                    rd=u.get('cache_read_input_tokens', 0) or 0,
                    out=u.get('output_tokens', 0) or 0,
                    think=(u.get('output_tokens_details') or {}).get('thinking_tokens', 0) or 0,
                    tools=n_tools, turn=turn,
                )
                if not cc and u.get('cache_creation_input_tokens'):
                    rec['w5'] = u['cache_creation_input_tokens']
                mid = msg.get('id')
                if mid is not None and seen is not None and mid in seen and mid not in by_id:
                    pending = []                       # counted in an earlier transcript of this report
                    continue
                if mid is not None and mid in by_id:
                    # the same message streamed in several lines: the last usage wins, the tools add up
                    prev = calls[by_id[mid]]
                    prev.update({k: rec[k] for k in ('inp', 'w5', 'w1', 'rd', 'out', 'think')})
                    prev['tools'] += n_tools
                    continue
                if mid is not None:
                    by_id[mid] = len(calls)
                rec['items'] = pending
                pending = []
                calls.append(rec)
    if seen is not None:
        seen.update(by_id)
    turn_tools = collections.Counter()
    for c in calls:
        turn_tools[c['turn']] += c['tools']
    for c in calls:
        c['turn_tools'] = turn_tools[c['turn']]
        c['fam'] = family(c['model'])
        c['ctx'] = c['inp'] + c['w5'] + c['w1'] + c['rd']
        c['cost'] = cost_of(c, c['fam'])
        c['total'] = sum(c['cost'].values())
    return dict(calls=calls, compactions=compactions, stop_hooks=stop_hooks, usage_limit=usage_limit,
                trigger_counts=dict(trigger_counts), cross_messages=cross_messages,
                attribution=attribute(calls))


def attribute(calls):
    """Who put the new tokens of each call into the context, and what re-reading them cost afterwards.

    The new tokens of a call (everything that is not a cache read) are split between: the start of the
    segment, the model's own previous response, a cache rewrite (a jump far bigger than what entered the
    context) and the items that entered it (tool results, messages, attachments), in proportion to size.
    """
    by_segment = collections.defaultdict(list)
    for i, c in enumerate(calls):
        by_segment[c['seg']].append(i)
    attr = collections.defaultdict(lambda: dict(tok=0.0, w_cost=0.0, rd_tok=0.0, rd_cost=0.0))
    for idx in by_segment.values():
        n = len(idx)
        for pos, i in enumerate(idx):
            c = calls[i]
            p = PRICES[c['fam']]
            after = n - pos - 1                       # how many later calls re-read these tokens
            new = c['inp'] + c['w5'] + c['w1']
            write_price = (c['inp'] * p['inp'] + c['w5'] * p['w5'] + c['w1'] * p['w1']) / max(new, 1)
            parts = []
            if pos == 0:
                parts.append((LABEL_BASE, float(new)))
            else:
                prev_out = calls[idx[pos - 1]]['out']
                est = sum(ch for _, ch in c['items']) / CHARS_PER_TOKEN
                rest = max(new - prev_out, 0)
                if est + prev_out + 20000 < rest / 2:      # a rewrite of the cache (expired or invalidated)
                    parts.append((LABEL_REWRITE, rest - est))
                    rest = est
                parts.append((LABEL_RESPONSE, float(min(prev_out, new))))
                total_chars = sum(ch for _, ch in c['items'])
                if total_chars > 0:
                    for label, ch in c['items']:
                        parts.append((label, rest * ch / total_chars))
                elif rest > 0:
                    parts.append((LABEL_UNATTRIBUTED, float(rest)))
            for label, tok in parts:
                a = attr[label]
                a['tok'] += tok
                a['w_cost'] += tok * write_price / 1e6
                if label == LABEL_REWRITE:
                    continue                           # the old context was already paid for elsewhere
                a['rd_tok'] += tok * after
                a['rd_cost'] += tok * after * p['rd'] / 1e6
    return dict(attr)


def summarize(result):
    calls = result['calls']
    if not calls:
        return None
    tokens = collections.Counter()
    cost = collections.Counter()
    for c in calls:
        for k in TOKEN_KEYS + ('think',):
            tokens[k] += c[k]
        for k, v in c['cost'].items():
            cost[k] += v
    cost['total'] = sum(cost[k] for k in TOKEN_KEYS)
    ctx = [c['ctx'] for c in calls]
    edges = ((1e5, '<100k'), (2e5, '100-200k'), (4e5, '200-400k'), (6e5, '400-600k'), (8e5, '600-800k'))
    buckets = collections.OrderedDict((name, dict(calls=0, cost=0.0)) for _, name in edges)
    buckets['>800k'] = dict(calls=0, cost=0.0)
    for c in calls:
        name = next((nm for edge, nm in edges if c['ctx'] < edge), '>800k')
        buckets[name]['calls'] += 1
        buckets[name]['cost'] += c['total']
    by_trigger = collections.defaultdict(lambda: dict(calls=0, cost=0.0))
    for c in calls:
        by_trigger[c['trigger']]['calls'] += 1
        by_trigger[c['trigger']]['cost'] += c['total']
    pauses = sum(1 for a, b in zip(calls, calls[1:]) if seconds_between(a['ts'], b['ts']) > 3600)
    return dict(
        calls=len(calls), first=calls[0]['ts'], last=calls[-1]['ts'], tokens=dict(tokens), cost=dict(cost),
        cache_read_share=cost['rd'] / cost['total'] if cost['total'] else 0.0,
        context_max=max(ctx), context_mean=sum(ctx) / len(ctx), context_buckets=dict(buckets),
        by_trigger=dict(by_trigger), models=dict(collections.Counter(c['model'] for c in calls)),
        efforts=dict(collections.Counter(c['effort'] for c in calls)), pauses_over_1h=pauses,
        compactions=result['compactions'], stop_hooks=result['stop_hooks'], usage_limit=result['usage_limit'],
        trigger_events=result['trigger_counts'])


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
        subs = []
        for f in sorted(glob.glob(os.path.join(os.path.dirname(path), sid, 'subagents', 'agent-*.jsonl'))):
            meta = {}
            meta_file = f[:-len('.jsonl')] + '.meta.json'
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, encoding='utf-8') as fh:
                        meta = json.load(fh)
                except Exception:
                    meta = {}
            subs.append(dict(path=f, meta=meta))
        sessions.append(dict(id=sid, path=path, subagents=subs))
    return sessions


# --- scenarios ---

def idle_rerun(c):
    """A call of a /goal re-run turn in which no call used a tool.

    Decided on the whole turn: a re-run that uses a tool and then answers with text alone did work.
    """
    return c['trigger'] == RERUN and c.get('turn_tools', c['tools']) == 0


def cap_reruns(calls, k):
    """Drop the idle /goal re-run turns beyond k in a row (None = no cap)."""
    if k is None:
        return calls
    kept, run, turn = [], 0, None
    for i, c in enumerate(calls):
        t = c.get('turn', ('call', i))           # without turn numbers every call is its own turn
        if t != turn:
            turn = t
            run = run + 1 if idle_rerun(c) else 0
        if idle_rerun(c) and run > k:
            continue
        kept.append(c)
    return kept


def simulate(calls, threshold, ttl):
    """Cost of the calls if the context were compacted at `threshold` tokens, with a cache of `ttl` ('5m' or '1h').

    The context grows by the same amount per call as in the real transcript. Writes are priced at the TTL's
    write price; when more time than the TTL passes between two calls the whole context is written again.
    """
    cost, sim, prev = 0.0, 0, None
    for c in calls:
        p = PRICES[c['fam']]
        write = p['w1'] if ttl == '1h' else p['w5']
        lifetime = 3600 if ttl == '1h' else 300
        if prev is None or c['seg'] != prev['seg']:
            sim = c['ctx']
            cost += c['ctx'] * write / 1e6 + c['out'] * p['out'] / 1e6
            prev = c
            continue
        delta = max(c['ctx'] - prev['ctx'], 0)
        if sim + delta > threshold:
            cost += sim * p['rd'] / 1e6 + SUMMARY_OUTPUT_TOKENS * p['out'] / 1e6 + POST_COMPACTION_CONTEXT * write / 1e6
            sim = POST_COMPACTION_CONTEXT
        if seconds_between(prev['ts'], c['ts']) > lifetime:
            cost += (sim + delta) * write / 1e6
        else:
            cost += sim * p['rd'] / 1e6 + delta * write / 1e6
        cost += c['out'] * p['out'] / 1e6
        sim += delta
        prev = c
    return cost


def idle_rerun_peak(calls, window_s=3600):
    """(most idle /goal re-runs inside any window, window / count = average seconds between them).

    With 298 re-runs in an hour that is 3600 / 298 = one every 12 seconds, however they were bunched.
    """
    starts, counted = [], set()
    for i, c in enumerate(calls):
        t = c.get('turn', ('call', i))
        if idle_rerun(c) and c['ts'] and t not in counted:   # one per re-run turn, at its first call
            counted.add(t)
            starts.append(parse_ts(c['ts']))
    times = sorted(starts)
    best, lo = 0, 0
    for hi in range(len(times)):
        while (times[hi] - times[lo]).total_seconds() > window_s:
            lo += 1
        best = max(best, hi - lo + 1)
    return (best, window_s / best) if best else (0, None)


def run_scenarios(main_calls, sub_calls, thresholds, rerun_cap, combined_threshold, main_ttl, sub_ttl):
    """main_calls / sub_calls: lists (one per transcript) of call lists."""
    def total(threshold=NO_COMPACTION, cap=None, sub_cache=sub_ttl):
        m = sum(simulate(cap_reruns(calls, cap), threshold, main_ttl) for calls in main_calls)
        s = sum(simulate(calls, threshold, sub_cache) for calls in sub_calls)
        return m, s

    def label(t):
        return f'{t // 1000}k'

    plan = [('Baseline (model of the transcripts as they are)', total())]
    for t in thresholds:
        plan.append((f'Compaction at {label(t)}', total(threshold=t)))
    if rerun_cap is not None:
        plan.append((f'Cap of {rerun_cap} idle goal re-runs', total(cap=rerun_cap)))
    plan.append(('Subagent cache TTL 1h', total(sub_cache='1h')))
    if combined_threshold:
        plan.append((f'Compaction at {label(combined_threshold)}'
                     + (f' + cap of {rerun_cap}' if rerun_cap is not None else '') + ' + subagent cache TTL 1h',
                     total(threshold=combined_threshold, cap=rerun_cap, sub_cache='1h')))
    base_m, base_s = plan[0][1]

    def saved(now, before):
        return round(100 * (1 - now / before), 1) if before else 0

    return [dict(name=name, main=m, subagents=s, total=m + s,
                 saved_vs_baseline_pct=saved(m + s, base_m + base_s),
                 saved_main_pct=saved(m, base_m), saved_subagents_pct=saved(s, base_s))
            for name, (m, s) in plan]


def reprice_as_sonnet(summaries):
    """What the main sessions would have cost at Sonnet prices (same tokens)."""
    p = PRICES['sonnet']
    actual = sum(s['cost']['total'] for s in summaries)
    repriced = sum(sum(s['tokens'][k] * p[k] / 1e6 for k in TOKEN_KEYS) for s in summaries)
    return dict(actual=actual, cost=repriced, saved_pct=round(100 * (1 - repriced / actual), 1) if actual else 0)


# --- the report ---

def build_report(sessions, since=None, until=None, thresholds=(500_000, 400_000, 300_000, 200_000),
                 rerun_cap=DEFAULT_RERUN_CAP,
                 combined_threshold=400_000, main_ttl='1h', sub_ttl='5m'):
    report_sessions, main_calls, sub_calls = {}, [], []
    main_summaries, trigger_events = [], collections.Counter()
    idle_cost = sub_cost = sub_rewrite = 0.0
    peak, peak_gap, n_subs, sub_total_calls, idle_calls, idle_turns = 0, None, 0, 0, 0, 0
    seen = set()
    for s in sessions:
        r = analyze_transcript(s['path'], since, until, seen)
        summary = summarize(r)
        subs = []
        for sub in s['subagents']:
            rr = analyze_transcript(sub['path'], since, until, seen)
            ss = summarize(rr)
            if not ss:
                continue
            sub_calls.append(rr['calls'])
            sub_cost += ss['cost']['total']
            rewrite = rr['attribution'].get(LABEL_REWRITE, {}).get('w_cost', 0.0)
            sub_rewrite += rewrite
            n_subs += 1
            sub_total_calls += ss['calls']
            subs.append(dict(file=os.path.basename(sub['path']), type=sub['meta'].get('agentType'),
                             description=sub['meta'].get('description'), summary=ss, cache_rewrite_cost=rewrite))
        if not summary and not subs:
            continue
        if summary:
            main_calls.append(r['calls'])
            main_summaries.append(summary)
            trigger_events.update(r['trigger_counts'])
            idle = [c for c in r['calls'] if idle_rerun(c)]
            idle_calls += len(idle)
            idle_turns += len({c['turn'] for c in idle})
            idle_cost += sum(c['total'] for c in idle)
            n, gap = idle_rerun_peak(r['calls'])
            if n > peak:
                peak, peak_gap = n, gap
        report_sessions[s['id']] = dict(main=summary, attribution=r['attribution'], subagents=subs,
                                        messages_from_other_sessions=len(r['cross_messages']))
    main_cost = sum(m['cost']['total'] for m in main_summaries)
    main_rd = sum(m['cost']['rd'] for m in main_summaries)
    totals = dict(
        main_calls=sum(m['calls'] for m in main_summaries), main_cost=main_cost,
        subagents=n_subs, subagent_calls=sub_total_calls, subagent_cost=sub_cost,
        total_cost=main_cost + sub_cost,
        main_cache_read_share=main_rd / main_cost if main_cost else 0.0,
        trigger_cost_shares=trigger_cost_shares(main_calls),
        trigger_events=dict(trigger_events), trigger_shares=trigger_shares(trigger_events),
        idle_goal_reruns=idle_turns, idle_goal_calls=idle_calls, idle_goal_cost=idle_cost,
        idle_goal_share_of_total=idle_cost / (main_cost + sub_cost) if main_cost + sub_cost else 0.0,
        peak_idle_reruns_per_hour=peak, peak_idle_rerun_interval_s=peak_gap,
        subagent_cache_rewrite_share=sub_rewrite / sub_cost if sub_cost else 0.0)
    report = dict(filters=dict(since=since.isoformat() if since else None, until=until.isoformat() if until else None),
                  prices=PRICES, sessions=report_sessions, totals=totals)
    if main_calls or sub_calls:
        report['scenarios'] = run_scenarios(main_calls, sub_calls, list(thresholds), rerun_cap, combined_threshold,
                                            main_ttl, sub_ttl)
    if main_summaries:
        report['main_as_sonnet'] = reprice_as_sonnet(main_summaries)
    return report


def pct(x):
    return f'{100 * x:.0f}%'


def print_report(rep, out=None):
    out = out or sys.stdout
    w = lambda s='': print(s, file=out)
    t = rep['totals']
    w('Cost is in USD at the list prices of the PRICES table (a weight, not a bill).')
    w()
    w(f"{'session':<38} {'calls':>7} {'cost':>10} {'cache reads':>12} {'ctx max':>9} {'subagents':>10}")
    for sid, s in rep['sessions'].items():
        m = s['main']
        sub_cost = sum(x['summary']['cost']['total'] for x in s['subagents'])
        if m:
            w(f"{sid[:38]:<38} {m['calls']:>7} {m['cost']['total']:>10.2f} {pct(m['cache_read_share']):>12} "
              f"{m['context_max'] / 1000:>8.0f}k {len(s['subagents']):>4} ({sub_cost:.2f})")
        else:
            w(f"{sid[:38]:<38} {'-':>7} {'-':>10} {'-':>12} {'-':>9} {len(s['subagents']):>4} ({sub_cost:.2f})")
    w()
    w(f"Main sessions: {t['main_calls']} calls, cost {t['main_cost']:.2f}. "
      f"Subagents: {t['subagents']} transcripts, {t['subagent_calls']} calls, cost {t['subagent_cost']:.2f}.")
    w(f"Cache reads are {pct(t['main_cache_read_share'])} of the main-session cost.")
    if t['trigger_cost_shares']:
        w()
        w('What started the turns (share of main-session cost, all the calls of each turn):')
        for group, share in t['trigger_cost_shares'].items():
            w(f'  {group:<14} {share:5.1f}%')
        w('  ... and by number of events (share of events, one per message that started a turn):')
        w('  ' + ', '.join(f'{group} {share:.1f}%' for group, share in t['trigger_shares'].items()))
    if t['idle_goal_reruns']:
        w()
        w(f"Idle /goal re-runs (turns without any tool use): {t['idle_goal_reruns']} turns, "
          f"{t['idle_goal_calls']} calls, cost {t['idle_goal_cost']:.2f} "
          f"({pct(t['idle_goal_share_of_total'])} of the total). Busiest hour: {t['peak_idle_reruns_per_hour']} re-runs"
          + (f", one every {t['peak_idle_rerun_interval_s']:.0f} s." if t['peak_idle_rerun_interval_s'] else '.'))
    if t['subagents']:
        w()
        w(f"Cache rewrites are {pct(t['subagent_cache_rewrite_share'])} of the subagent cost.")
    if 'main_as_sonnet' in rep:
        m = rep['main_as_sonnet']
        w()
        w(f"Main sessions priced as Sonnet (same tokens): {m['cost']:.2f} instead of {m['actual']:.2f} ({m['saved_pct']:.0f}% less).")
    if 'scenarios' in rep:
        w()
        w('Scenarios (the same transcripts replayed with other settings; the baseline is the model of the')
        w(f"transcripts as they are, actual cost {t['total_cost']:.2f}; the percentages compare with the baseline):")
        w(f"  {'scenario':<64} {'main':>9} {'main %':>7} {'subagents':>10} {'total':>9} {'total %':>8}")
        for s in rep['scenarios']:
            w(f"  {s['name']:<64} {s['main']:>9.2f} {0 - s['saved_main_pct']:>6.0f}% {s['subagents']:>10.2f} "
              f"{s['total']:>9.2f} {0 - s['saved_vs_baseline_pct']:>7.0f}%")


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


def thresholds_arg(s):
    try:
        return [int(x) for x in s.split(',') if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a list of integers: {s!r}')


def build_parser():
    ap = argparse.ArgumentParser(description='Analyze Claude Code session transcripts: where do the tokens go?')
    ap.add_argument('--projects-dir', default=os.path.join('~', '.claude', 'projects'),
                    help='Claude Code projects folder, or one project folder (default ~/.claude/projects)')
    ap.add_argument('--since', type=when_arg(False), help='only calls from this date/time on (UTC if no zone)')
    ap.add_argument('--until', type=when_arg(True), help='only calls up to this date/time (a plain date includes the day)')
    ap.add_argument('--session', action='append', help='session id (the file name without .jsonl); repeatable')
    ap.add_argument('--out', help='write the full report as JSON to this file')
    ap.add_argument('--thresholds', type=thresholds_arg, default=[500_000, 400_000, 300_000, 200_000],
                    help='compaction thresholds to simulate, in tokens (comma separated)')
    ap.add_argument('--rerun-cap', type=int, default=DEFAULT_RERUN_CAP,
                    help=f'cap on idle /goal re-runs to simulate (default {DEFAULT_RERUN_CAP})')
    ap.add_argument('--combined-threshold', type=int, default=400_000,
                    help='threshold of the combined scenario (compaction + cap + 1h subagent cache); 0 = skip')
    ap.add_argument('--main-cache-ttl', choices=('5m', '1h'), default='1h', help='cache TTL of the main sessions (default 1h)')
    ap.add_argument('--subagent-cache-ttl', choices=('5m', '1h'), default='5m', help='cache TTL of the subagents today (default 5m)')
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    sessions = discover_sessions(args.projects_dir, set(args.session) if args.session else None)
    if not sessions:
        print(f'No sessions found under {args.projects_dir}')
        return 1
    rep = build_report(sessions, args.since, args.until, args.thresholds, args.rerun_cap,
                       args.combined_threshold, args.main_cache_ttl, args.subagent_cache_ttl)
    if not rep['sessions']:
        print('No calls found in the selected sessions and time range.')
        return 1
    print_report(rep)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1, default=str)
        print(f'\nFull report written to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
