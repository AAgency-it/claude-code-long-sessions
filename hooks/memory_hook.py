"""Session memory across compaction (a Claude Code hook).

Every long session keeps a short state file (see templates/STATE_TEMPLATE.md) in a state folder.
This script links the file to the session, reminds the model to update it before compaction
cuts the context, and after compaction puts it back into the context, read from disk.

One command per event:
  after-tool    PostToolUse     links the state file when the session writes it;
                                at a high context size, reminds the model to update it.
  pre-compact   PreCompact      records the compaction. Never blocks it.
  post-summary  PostCompact     records whether the summary mentions the state file.
  after-compact SessionStart    (matcher "compact") puts the state file back into the context.

Golden rule: the hook must never break a session. Any error: no output, exit code 0,
one entry in errors.log.

Environment variables (all optional):
  MEMORY_STATE_DIR       folder with the state files            (default ~/.claude/session-state)
  MEMORY_DATA_DIR        folder with the hook's own bookkeeping (default ~/.claude/memory-sessions)
  MEMORY_FIRST_MARGIN    first reminder at window - this many tokens   (default 100000)
  MEMORY_URGENT_MARGIN   urgent reminder at window - this many tokens  (default 60000)
  MEMORY_THRESHOLD       absolute context size of the first reminder (for tests)
  CLAUDE_CODE_AUTO_COMPACT_WINDOW  the window the session runs with  (default 1000000)
"""
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

HOME = os.path.expanduser('~')
# No shell expands ~ in the env block of settings.json, so the hook does it.
STATE_DIR = os.path.expanduser(os.environ.get('MEMORY_STATE_DIR') or os.path.join(HOME, '.claude', 'session-state'))
DATA = os.path.expanduser(os.environ.get('MEMORY_DATA_DIR') or os.path.join(HOME, '.claude', 'memory-sessions'))
TEMPLATE = os.path.join(STATE_DIR, '_TEMPLATE.md')

# Automatic compaction fires about 33,000 tokens before the window (1M -> ~967k, measured).
COMPACTION_MARGIN = 33_000
def env_int(name, default):
    """An integer from the environment; a missing, empty or non-numeric value gives the default."""
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


FIRST_MARGIN = env_int('MEMORY_FIRST_MARGIN', 100_000)
URGENT_MARGIN = env_int('MEMORY_URGENT_MARGIN', 60_000)
REMINDER_PAUSE_S = 10 * 60
STATE_FRESH_S = 15 * 60
STATE_FRESH_URGENT_S = 5 * 60
STATE_OLD_S = 2 * 3600
CONTEXT_CAP = 9_500             # Claude Code cuts additionalContext at 10,000 characters
WRITE_TOOLS = {'Write', 'Edit', 'MultiEdit'}


def now():
    return time.time()


def stamp(t=None):
    return datetime.fromtimestamp(t if t is not None else now()).strftime('%Y-%m-%d %H:%M')


def window():
    return env_int('CLAUDE_CODE_AUTO_COMPACT_WINDOW', 1_000_000)


def thresholds():
    """(first reminder, urgent from, expected compaction), in context tokens."""
    w = window()
    first = env_int('MEMORY_THRESHOLD', w - FIRST_MARGIN)
    return first, max(first, w - URGENT_MARGIN), w - COMPACTION_MARGIN


def k(n):
    return f'{round(n / 1000)}k'


def normalize(p):
    """A comparable path: accepts C:\\..., C:/... and, on Windows, /c/... (Git Bash)."""
    if not p:
        return ''
    return os.path.normcase(real_path(p))


def real_path(p, windows=None):
    """The path with its original case. On Windows it also accepts /c/... from Git Bash.

    Elsewhere /c/... is an ordinary path and is left alone. `windows` is for tests; by default it is
    decided by the running system.
    """
    if not p:
        return ''
    if windows is None:
        windows = os.name == 'nt'
    p = str(p).strip().strip('"')
    m = re.match(r'^/([a-zA-Z])/(.*)$', p) if windows else None
    if m:
        p = f'{m.group(1).upper()}:/{m.group(2)}'
    return os.path.normpath(p)


def is_state_file(p):
    n = normalize(p)
    base = normalize(STATE_DIR)
    name = os.path.basename(n)
    return n.startswith(base + os.sep) and n.endswith('.md') and not name.startswith('_')


# --- per-session bookkeeping: one JSON file per session_id, written atomically ---

def data_file(sid):
    sid = re.sub(r'[^A-Za-z0-9_-]', '', sid or '')
    return os.path.join(DATA, (sid or 'no-id') + '.json')


def load(sid):
    try:
        with open(data_file(sid), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def save(sid, d):
    os.makedirs(DATA, exist_ok=True)
    dest = data_file(sid)
    tmp = dest + f'.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, dest)


# --- reading the transcript from its tail ---

def tail(path, n_bytes):
    with open(path, 'rb') as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - n_bytes))
        raw = fh.read()
    return raw.decode('utf-8', 'replace'), size <= n_bytes


def current_context(transcript):
    """Context tokens of the last model call of the main session (None if not found)."""
    if not transcript or not os.path.exists(transcript):
        return None
    for n_bytes in (1 << 20, 8 << 20):
        text, whole = tail(transcript, n_bytes)
        lines = text.split('\n')
        if not whole:
            lines = lines[1:]           # the first line may be cut in half
        for line in reversed(lines):
            if '"compact_boundary"' in line:
                return None             # just compacted: the real size is not written yet
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get('type') != 'assistant' or o.get('isSidechain'):
                continue
            msg = o.get('message') or {}
            if msg.get('model') == '<synthetic>':
                continue
            u = msg.get('usage') or {}
            total = (u.get('input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0)
            if total:
                return total
        if whole:
            break
    return None


def state_from_transcript(transcript):
    """The last state file this session wrote (Write, Edit, MultiEdit) that still exists on disk.

    Only writes count: a session that merely read another session's state file must not take it as its own.
    """
    if not transcript or not os.path.exists(transcript):
        return None
    text, _ = tail(transcript, 8 << 20)
    for line in reversed(text.splitlines()):
        if '"tool_use"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue                                   # the first line of the tail can be cut
        if not isinstance(o, dict) or o.get('type') != 'assistant' or o.get('isSidechain'):
            continue
        for b in reversed((o.get('message') or {}).get('content') or []):
            if not (isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') in WRITE_TOOLS):
                continue
            p = real_path((b.get('input') or {}).get('file_path') or '')
            if p and is_state_file(p) and os.path.exists(p):
                return p
    return None


def session_state(sid, transcript, d):
    p = d.get('state')
    if p and os.path.exists(p):
        return p
    p = state_from_transcript(transcript)
    if p:
        d['state'] = p
        d['state_registered'] = stamp()
        d['state_origin'] = 'transcript'
        save(sid, d)
    return p


def emit(event, text):
    sys.stdout.write(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}, ensure_ascii=True))
    sys.stdout.flush()


# --- commands ---

def after_tool(inp):
    if inp.get('agent_id'):
        return                          # subagents do not keep a state file
    sid = inp.get('session_id')
    d = load(sid)
    if inp.get('tool_name') in WRITE_TOOLS:
        fp = (inp.get('tool_input') or {}).get('file_path')
        if fp and is_state_file(fp):
            new = real_path(fp)
            old = d.get('state')
            if old and os.path.exists(old) and normalize(old) != normalize(new):
                others = d.setdefault('other_states_written', [])
                if new not in others:
                    others.append(new)      # the first state file of the session wins, while it exists
            else:
                d['state'] = new
                d['state_registered'] = stamp()
                d['state_origin'] = 'write'
            save(sid, d)
            return
    ctx = current_context(inp.get('transcript_path'))
    if ctx is None:
        return
    first, urgent_from, fires = thresholds()
    if ctx < first:
        return
    urgent = ctx >= urgent_from
    state = session_state(sid, inp.get('transcript_path'), d)
    t = now()
    if state and t - os.path.getmtime(state) < (STATE_FRESH_URGENT_S if urgent else STATE_FRESH_S):
        return
    key = 'last_urgent' if urgent else 'last_reminder'
    if t - (d.get(key) or 0) < REMINDER_PAUSE_S:
        return
    d[key] = t
    d.setdefault('reminders', []).append({'time': stamp(t), 'context': ctx, 'urgent': urgent})
    d['reminders'] = d['reminders'][-50:]
    save(sid, d)
    head = 'MEMORY REMINDER, URGENT' if urgent else 'MEMORY REMINDER'
    if state:
        ago = round((t - os.path.getmtime(state)) / 60)
        text = (f'{head} (hook): the context is at about {k(ctx)} tokens and automatic compaction fires around {k(fires)}. '
                f"It's a working rule, not a suggestion: before you continue, update your state file {state} (last updated {ago} minutes ago): "
                'Done, Decisions, Waiting on, Next step, Re-read. Rewrite it with Write or Edit, do not make it longer; '
                'keep it under 8,000 characters. Then go back to work.')
    else:
        text = (f'{head} (hook): the context is at about {k(ctx)} tokens and automatic compaction fires around {k(fires)}. '
                f"This session has no state file. It's a working rule, not a suggestion: create it now with Write in "
                f'{STATE_DIR}{os.sep}<session-name>.md, following the template {TEMPLATE}, under 8,000 characters. '
                'After compaction it will be put back into your context automatically.')
    emit('PostToolUse', text)


def pre_compact(inp):
    if inp.get('agent_id'):
        return
    sid = inp.get('session_id')
    d = load(sid)
    d.setdefault('compactions', []).append({
        'time': stamp(), 'trigger': inp.get('trigger'), 'context': current_context(inp.get('transcript_path')),
        'state': d.get('state'),
        'state_updated': stamp(os.path.getmtime(d['state'])) if d.get('state') and os.path.exists(d['state']) else None,
    })
    d['compactions'] = d['compactions'][-50:]
    save(sid, d)


def post_summary(inp):
    if inp.get('agent_id'):
        return
    sid = inp.get('session_id')
    d = load(sid)
    summary = inp.get('compact_summary') or ''
    state = d.get('state')
    cites = bool(state) and (os.path.basename(state) in summary or os.path.basename(os.path.normpath(STATE_DIR)) in summary)
    comp = d.setdefault('compactions', [])
    if not comp:
        comp.append({'time': stamp(), 'trigger': inp.get('trigger')})
    comp[-1]['summary_chars'] = len(summary)
    comp[-1]['summary_mentions_state'] = cites
    save(sid, d)


def after_compact(inp):
    if inp.get('agent_id'):
        return
    if inp.get('source') not in (None, 'compact'):
        return
    sid = inp.get('session_id')
    d = load(sid)
    state = session_state(sid, inp.get('transcript_path'), d)
    last = (d.get('compactions') or [{}])[-1]
    how = {'auto': 'automatic', 'manual': 'manual'}.get(last.get('trigger'), '')
    head = f'MEMORY AFTER COMPACTION (hook): the conversation was just compacted{(" (" + how + ")") if how else ""}.'
    if not state:
        emit('SessionStart', head + (
            ' This session has no state file. If the work continues: create it now with Write in '
            f'{STATE_DIR}{os.sep}<session-name>.md following the template {TEMPLATE}, and re-read any handoff '
            'note or plan you were working from before you resume.'))
        return
    mtime = os.path.getmtime(state)
    age = now() - mtime
    try:
        with open(state, encoding='utf-8') as fh:
            content = fh.read()
    except UnicodeDecodeError:
        with open(state, encoding='cp1252', errors='replace') as fh:
            content = fh.read()
    instructions = (f' Your state file is {state}, updated on {stamp(mtime)} ({round(age / 60)} minutes ago); '
                    'its content, read from disk, is below. Before you continue: resume from the "Next step"; '
                    're-read the files under "Re-read" that this step needs; if the summary and the state file '
                    'disagree, the more recent one wins and you correct the other.')
    if age > STATE_OLD_S:
        instructions += f' Warning: the state file is {round(age / 3600, 1)} hours old; as soon as you have picked the thread up again, update it.'
    header = head + instructions + '\n\n--- STATE FILE ---\n'
    footer = '\n--- END OF STATE FILE ---'
    room = CONTEXT_CAP - len(header) - len(footer)
    if len(content) > room:
        cut = f'\n[... truncated: the file has more text, open it with Read: {state}]'
        content = content[:max(room - len(cut), 0)].rstrip() + cut
    emit('SessionStart', header + content + footer)


COMMANDS = {
    'after-tool': after_tool,
    'pre-compact': pre_compact,
    'post-summary': post_summary,
    'after-compact': after_compact,
}


def main():
    try:
        command = COMMANDS.get(sys.argv[1] if len(sys.argv) > 1 else '')
        raw = sys.stdin.buffer.read().decode('utf-8', 'replace')
        if not command or not raw.strip():
            return
        command(json.loads(raw))
    except Exception:
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(os.path.join(DATA, 'errors.log'), 'a', encoding='utf-8') as fh:
                fh.write(f'--- {stamp()} {sys.argv[1:]}\n{traceback.format_exc()}\n')
        except Exception:
            pass


if __name__ == '__main__':
    main()
    sys.exit(0)
