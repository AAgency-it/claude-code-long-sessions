# claude-code-long-sessions

A small kit for people who run **long Claude Code sessions**, or **several sessions at once**. It does
two things: it keeps your token cost down, and it keeps the session from losing the thread when the
conversation is compacted.

Italiano: [README.it.md](README.it.md)

- **`hooks/memory_hook.py`**: a hook that makes each session keep a short *state file*, reminds the model
  to update it before compaction, and puts it back into the context right after compaction.
- **`tools/analyze_usage.py`**: reads your own transcripts and shows where the tokens went, and what other
  settings would have cost.
- **Templates and notes**: a state-file template, a "Compact instructions" section for your `CLAUDE.md`,
  the settings we verified, and a procedure for one reviewer session driving several worker sessions.

Python 3.10 or newer, standard library only. Tested on Windows (where the hook also understands Git Bash
paths like `/c/Users/...`); it is written for macOS and Linux too, but we have not run it there.

This is an independent project, not affiliated with or endorsed by Anthropic.

## The problem

A long session is expensive for a reason that is easy to miss: **every model call re-reads the whole
context** from the cache. The longer the context, the more each tiny step costs, and nothing you do in a
single turn changes that. And when the context is finally compacted, the summary drops details that you
needed: a decision, a path, a constraint.

## What we measured

One real case, from our own usage: **5 main sessions and 34 subagents in 25 hours, 10,530 calls to the
API, Claude Code 2.1.302.** Your numbers will differ; `tools/analyze_usage.py` measures yours.

- **78%** of the cost of the main sessions was re-reading the cached context.
- What started the turns, weighed on the cost of the main sessions: **48%** messages from other
  sessions, **33%** re-runs of the `/goal`, **9%** notifications, **8%** the person.
- **1,167** idle re-runs of the `/goal` (turns without any tool use), about **11%** of the total spend. A
  reviewer waiting for the others was re-run **294 times in one hour**, once every 12 seconds.
- Compacting earlier, simulated instead of at about 967k, on the cost of the main sessions: **-40%** at
  500k, **-50%** at 400k, **-56%** at 300k, **-61%** at 200k.
- Subagents: with the 5-minute cache, **38%** of their cost was rewriting a cache that had expired or been
  invalidated.
- Moving the main sessions from Opus to Sonnet was worth only about **11%**: reading the cache costs the same.

The lesson: the big levers are not the model. They are *how long the context gets*, *who wakes a
session up*, and *how often a cache expires*.

## Install in 5 minutes

1. **Get the kit** (clone it or copy the folder) and note its absolute path.
2. **Create the state folder** and copy the template there:

   ```
   mkdir ~/.claude/session-state
   cp templates/STATE_TEMPLATE.md ~/.claude/session-state/_TEMPLATE.md
   ```

   (On Windows: `%USERPROFILE%\.claude\session-state`.) Files starting with `_` are never taken for state files.
3. **Add the hooks and the settings.** Merge `hooks/settings.example.json` into `~/.claude/settings.json`.
   Replace `/absolute/path/to/claude-code-long-sessions` with the real path; on Windows write it with
   forward slashes (`C:/Users/me/claude-code-long-sessions`). Use `python` (or `python3`, or `py -3` on
   Windows) in the commands, whichever starts Python 3.10+ on your machine. Check with `/hooks` inside
   Claude Code that the four hooks are listed.
4. **Paste the compact instructions.** Copy `templates/CLAUDE.compact-instructions.md` into your
   `CLAUDE.md`. It makes every summary start with the path of the state file and keep what matters.
5. **Check it works.** `python scripts/integration_check.py` runs a small end-to-end test with the Haiku
   model (3 short calls): it writes a state file, adds a secret code from outside, compacts, and asks the
   session for the code. Add `--dry-run` to see the commands first. The unit tests need no network:
   `python -m unittest discover -s tests`.
6. **Measure.** `python tools/analyze_usage.py` prints a table for the sessions under `~/.claude/projects`.

The `env` block of the example sets three things: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=400000` (compact
earlier), `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=3` (fewer idle `/goal` re-runs in a row) and
`CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL=1h` (a longer cache for subagents). Change them to taste; see
[docs/settings.md](docs/settings.md).

## How the memory hook works

Four commands, one per hook event:

| Command | Event | What it does |
|---|---|---|
| `after-tool` | `PostToolUse` | If the session just wrote a file in the state folder, remembers it as the session's state file. When the context nears compaction, reminds the model to update that file (or to create it). |
| `pre-compact` | `PreCompact` | Records the compaction. Never blocks it. |
| `post-summary` | `PostCompact` | Records whether the summary mentions the state file. |
| `after-compact` | `SessionStart` (matcher `compact`) | Puts the state file back into the context, read from disk, with the instruction to resume from its "Next step". |

The reminders are timed against the window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, 1,000,000 if unset):
a first reminder at window minus 100,000 tokens, an urgent one at window minus 60,000, and compaction
itself fires at about window minus 33,000. There are pauses between reminders, and no reminder if the
state file was updated recently. The wording is deliberately firm ("It's a working rule, not a
suggestion").

The state file (`templates/STATE_TEMPLATE.md`) is short, under 8,000 characters, and rewritten rather
than extended: goal and phase, done, decisions, waiting on, next step, files to re-read.

Safety rules of the hook:

- **It never breaks a session.** On any error it prints nothing, exits 0 and adds one entry to
  `errors.log` in the data folder.
- **Subagents are skipped.** They do not keep a state file.
- If the hook missed the moment the session wrote its state file, it looks in the transcript for the last
  state file the session wrote (Write or Edit). A state file the session only read, such as another
  session's, is never taken.

## Settings

The hook reads `MEMORY_STATE_DIR` (default `~/.claude/session-state`), `MEMORY_DATA_DIR` (default
`~/.claude/memory-sessions`), `MEMORY_FIRST_MARGIN`, `MEMORY_URGENT_MARGIN` and `MEMORY_THRESHOLD`.
Everything we checked about Claude Code's own variables (compaction window, `/goal` re-run cap, cache TTL
of subagents, the skill listing budget) is in [docs/settings.md](docs/settings.md), with the version it
was observed on.

## Several sessions at once

[docs/multi-session.md](docs/multi-session.md) describes how to run one *reviewer* session over several
*worker* sessions without paying for them to wake each other up: who holds the `/goal`, what a message
between sessions must contain, how to brief a subagent.

## Analyzing your usage

```
python tools/analyze_usage.py                              # everything under ~/.claude/projects
python tools/analyze_usage.py --since 2026-01-10 --until 2026-01-12 --out report.json
python tools/analyze_usage.py --session <session id> --session <another id>
```

It de-duplicates calls by message id (across all the transcripts of one run), separates main sessions from
subagents, and classifies what started each turn, weighed on cost: every call of a turn counts for its
trigger. A `/goal` re-run is idle when no call of its turn used a tool. It replays your transcripts with a
different compaction threshold (`--thresholds`), a cap on idle `/goal` re-runs (`--rerun-cap`, default 3)
and a 1-hour subagent cache; the scenario table shows the change on the main sessions and on the total.
With `--out report.json` it also writes who put the new tokens into the context (tool results, messages,
cache rewrites). Other options: `--projects-dir`, `--combined-threshold`, `--main-cache-ttl`,
`--subagent-cache-ttl` (see `--help`). Costs use the `PRICES` table at the top of the file: list prices at
the time of writing, so check the current ones. The result is a model of your usage, not a bill.

## Limits

- **Observed on Claude Code 2.1.302.** Some variables are undocumented and may change in other versions.
- **The reminder depends on tool calls.** The hook runs after every tool call; a session that answers for
  a long time without using a tool gets no reminder until its next tool call.
- **The hook needs to know your window.** It reads `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and assumes
  1,000,000 when it is not set. If your sessions compact earlier, set the variable (the example sets
  400000), or the reminders come too late or never.
- **It asks, it does not force.** The model writes the state file; the hook can only remind it and put the
  file back. A model can still ignore the reminder.
- **Lowering the window below the current context** makes compaction fire at the next call, with no time
  to write the state file.
- **Context cap.** Claude Code truncates the injected context at 10,000 characters; the hook stays under
  9,500 and cuts a longer state file with a pointer to open it.
- **The simulation is a model.** It assumes the context grows by the same amount per call and that a
  compaction leaves about 45,000 tokens. Use it to compare settings, not to predict a bill.
- **Our percentages come from one case.** A different workload gives different shares.

## Tests

```
python -m unittest discover -s tests
```

The tests run the real hook script the way Claude Code does (JSON on stdin, JSON on stdout) and analyze
synthetic transcripts in `tests/fixtures`.

## Credit and license

Built by AAgency from what we learned running long sessions. MIT license, see [LICENSE](LICENSE).
