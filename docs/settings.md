# Settings: what we verified

> Observed on Claude Code 2.1.302, re-checked on 2.1.310; some variables are undocumented and may change.

Everything below was checked on a real installation (with `/context`, with transcripts, or with an
end-to-end run of `claude -p`). Where we did not check something, this page says nothing about it.
`hooks/settings.example.json` contains the three environment variables we use.

## Auto-compaction

### `CLAUDE_CODE_AUTO_COMPACT_WINDOW`

The window, in tokens, that automatic compaction works against.

- Compaction fires about 33,000 tokens before the window (a 1,000,000 window compacts around 967k).
  This applies to subagents too.
- Where the variable is set matters:
  - the `env` block of `~/.claude/settings.json` overrides a variable passed to the process;
  - `--settings '{"env": {...}}'` overrides `settings.json`.

  We proved both with `/context`.
- Changes to `settings.json` reach sessions that are already open.
- If you lower the threshold below the current context size, compaction fires at the very next call.
  There is no time to write the state file. Lower it between tasks, not in the middle of one.

The memory hook reads this variable to know when to remind the model (first reminder at window minus
100,000 tokens, urgent at window minus 60,000, compaction expected at window minus 33,000).

Re-checked on 2.1.310 from the transcripts: every compaction writes a `compact_boundary` record whose
`compactMetadata` holds `preTokens` (the context it fired at), `postTokens` (the summary) and
`durationMs`. With a 220k window the median `preTokens` of 306 compactions was 187,847; with a 400k
window, 367,240. The summary was about 52k, but the first reply after it already carried about 145k:
Claude Code attaches the recently read files and other context again, so at 188k only about 43k was
left to work with. A compaction took 58 seconds on average. `tools/run_report.py` reads these
fields for you.

### Choosing the window

There is no free value; see [run-63h.md](run-63h.md) for the numbers.

- **Too high** (the default 1,000,000 compacts near 967k): every call re-reads a huge context. In our
  first run, re-reading the cache was 78% of the main-session cost.
- **Too low** (220k, compacting near 188k): the session restarts at about 145k, has about 43k of room and compacts every 25 turns or so. Each restart
  rewrites the cache, writes a summary, re-reads files, and the edits right after it fail more often. In
  our second run, cache writes became 49% of the cost and compactions took 5.3 hours.
- **What we use now:** 333,000 for working sessions (compaction near 300k, hook reminders at 233k and
  273k) and 400,000 for a reviewer that mostly reads (compaction near 367k). The example settings use
  333,000.

A launcher can set a different window per kind of session with `--settings '{"env": {...}}'`, which
overrides `settings.json`.

### "Compact instructions" in CLAUDE.md

A `## Compact instructions` section in CLAUDE.md applies to automatic compaction as well as to manual
`/compact`. We checked it on an automatic compaction: the summary started with the line the section
asked for. `templates/CLAUDE.compact-instructions.md` is a ready-made section.

## `/goal` and re-runs

`/goal` is a Stop hook of type `prompt`: at the end of each turn a small model reads the goal condition
and the conversation and decides whether the goal is reached. If not, it blocks the stop and the session
runs another turn.

- It does not evaluate while there is background work running.
- `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` caps how many times in a row the stop hook can block. The default is
  8. The count starts again when something new arrives, such as a message or a notification.
- Advice: give the `/goal` only to the session that can verify the end. Never give it to a session that
  is waiting for others: it cannot reach the end by itself, so every idle turn is a paid re-run of its
  whole context. In our data the idle re-runs were about 11% of the total spend (see the README).
- Write the end condition in the `/goal` line itself: the evaluator has no tools, so it cannot open a
  file that the line points to.

## Cache

### `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h`

Subagents use a 5-minute cache by default; with `1h` they use the 1-hour cache. The main session already
has 1 hour. The point: a subagent that pauses for more than 5 minutes (waiting for a long tool, or for
its caller) pays to rewrite its whole cached context. With 1 hour the rewrite happens far less often, at
a higher write price. The simulation in `tools/analyze_usage.py` (scenario "Subagent cache TTL 1h") tells
you what it would do to your own sessions.

## The skill listing

Every call carries a listing of the available skills. Its size is limited by:

- `SLASH_COMMAND_TOOL_CHAR_BUDGET`: an absolute budget, in characters. The default is the window x 4 x 0.01.
- `skillListingMaxDescChars`: the maximum length of one description. The default is 1536.

With a 1,000,000 window the default budget is 40,000 characters. We raised it to 100,000 so that about
184 skills all fit with their full descriptions; the listing then cost about 27,000 tokens per call. Full
descriptions help the model pick the right skill, and they are paid on every call: uninstall what you do
not use.

## Variables of the memory hook

Set in the environment of the hook (for example in the `env` block of the settings):

| Variable | Default | Meaning |
|---|---|---|
| `MEMORY_STATE_DIR` | `~/.claude/session-state` | where the state files live |
| `MEMORY_DATA_DIR` | `~/.claude/memory-sessions` | the hook's own bookkeeping, one JSON per session, and `errors.log` |
| `MEMORY_FIRST_MARGIN` | `100000` | first reminder at window minus this many tokens |
| `MEMORY_URGENT_MARGIN` | `60000` | urgent reminder at window minus this many tokens |
| `MEMORY_THRESHOLD` | unset | absolute context size of the first reminder (for tests) |

Fixed in the code: compaction margin 33,000 tokens; at least 10 minutes between reminders of the same
kind; a state file younger than 15 minutes (5 in the urgent case) needs no reminder; a state file older
than 2 hours is flagged after compaction; the injected text is capped at 9,500 characters because Claude
Code cuts `additionalContext` at 10,000.

## What we did not verify

- Other versions of Claude Code.
- Whether the variables above are honoured by every way of starting Claude Code (IDE extensions, SDK).
- The exact price table: `PRICES` in `tools/analyze_usage.py` holds list prices at the time of writing.
  Check the current ones.
