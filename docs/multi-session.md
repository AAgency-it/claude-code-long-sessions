# Several sessions at once: one reviewer, many workers

A procedure for when you run more than one long Claude Code session at the same time: several
"worker" sessions that each own a piece of the job, and one "reviewer" session that follows them. It
came out of a 25-hour run with five main sessions (see the README) and of what that run cost. Adapt it;
the numbers in it are starting points, not laws. It assumes your sessions have some way to send each
other messages and to notice when another session goes idle; use whatever your setup offers for that.

## 1. When you need a reviewer

- **Yes**, when the workers share something: the same database, the memory of one machine for renders,
  the window for pushes, a calendar. Yes also when you delegate technical go-aheads to someone.
- **No**, for a single worker on its own job. A reviewer subagent with a clean context, called at the
  key moments (on the plan, and before the push), is enough. It saves a whole extra long session.

## 2. Who holds the `/goal`

**Only the reviewer.** Workers start with an ordinary message, with no `/goal`. The reviewer's goal is
what carries the whole job to its end: it follows the workers and sets them moving again.

The reason is in `docs/settings.md`: a session that waits for others cannot reach its own end, so a
`/goal` on it only produces idle re-runs, each one re-reading the whole context. In the run we measured,
the reviewer waiting for the workers was re-run 294 times in one hour.

Write the end condition in the `/goal` line itself, because the small model that evaluates it reads only
that line and the conversation:

```
/goal Follow <path to the goal file>. The goal is reached when every worker has its final
outcome in the log (done, or BLOCKED with who, what and by when) and the final report is written.
If only an action from a person is missing, write BLOCKED with who and what and stop without checking again.
```

What keeps the cost down: at most 3 idle re-runs in a row (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=3`) and a
compaction threshold you choose on purpose (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`). Both are in
`hooks/settings.example.json`.

## 3. Starting

- Start every session from its own working folder, never from your home directory.
- The first act of every session is to create its state file from `templates/STATE_TEMPLATE.md`
  (`<state folder>/<session-name>.md`). The memory hook reminds it to keep it current and puts it back
  after each compaction.
- Once the workers are up, the reviewer asks each one to report its goal, its constraints and its
  subagents, and subscribes to their idle notifications.

## 4. The reviewer

- **It works on events.**
  - A message from a worker wakes it.
  - When a worker goes idle it gets a single notification, at no cost to the worker. It reads that
    worker's state file: if the worker is waiting for the reviewer, it answers; if the worker stopped
    for no reason, it says what to do. Then it subscribes again, because the notification is good once.
- **One check round per hour.** It reads the workers' state files, the memory and disk of the machine,
  and updates the log. It does not ask for status by message.
- **Go-aheads** are given on the exact commit, after a check of its own by a second method (for example:
  does the branch fast-forward, which shared files does the diff touch).
- **To the person** it writes only for strategic decisions and for things to look at, in one message
  that collects them.

## 5. The workers

- They work alone until the end of their phase.
- Every time they stop, they write ONE message to the reviewer. The message has a TYPE:
  `phase closed`, `BLOCKED`, `question`, or `delivery to approve`.
- They update their state file at every important step.

## 6. Messages between sessions

- **A message is cheap, the wake-up is not.** In our run the text of the messages between sessions,
  written into the context and re-read afterwards, was about 3% of the main-session cost; the turns those
  messages started were 48%. The real cost is the wake-up of the receiver, who re-reads its whole
  context. Say what is needed, but every message must carry information.
- **Format**, five lines plus numbered points:

  ```
  FROM/TO: <sender> -> <receiver>
  TYPE:    phase closed | BLOCKED | question | delivery to approve
  TASK:    <the task, one line>
  FILE:    <path to what to read>
  EXPECTED: <what you need back, and by when>
  ```
- **What a message is worth**: a go-ahead, a correction, a decision, a closed phase, a block, a question.
- **What is not sent**: receipts and acknowledgements; requests for status (status is read in the state
  file); constraints already written in the goal file; replies to a receipt.
- **Several things for the same session** go in one message.

## 7. Briefs for subagents

Every brief has seven parts:

1. **Goal**, and the **end** at a precise point: "tests green", "ready to render", "report written".
2. **Skill to load**: the exact name, or "none".
3. **Files to read and to touch**, and **files not to touch**.
4. **Exact, narrow test commands**, with the temporary folder on the right disk. The whole suite is run
   by the worker, not the subagent.
5. **Short output**: tests that print only the failures; logs to a file, read with the last 40 lines;
   the list of changed files first, then the diff; files read in pieces.
6. **Report in a file**: its path, and the format of the answer.
7. **Constraints**: the model, the working folder, no push.

Rules for all the work with subagents:

- **A new brief** if a subagent must be resumed: short and fresh. Do not keep an agent alive past 400k.
- **Contract first.** Before splitting work among several developers, write the contract between the
  parts, so the final reviewer does not find dozens of defects.
- **Renders** (any heavy job) go through one queue, with one go-ahead per batch.
- **A second review round** only if a small change can raise the grade by more than half a point.

## 8. How to write a goal file

1. **One end only, measurable**, which the session can check by itself. What depends on a person or a
   future date is not part of the end: write it as "BLOCKED: who, what, by when".
2. **One job per file.**
3. **Thresholds reachable with the material you have.** And a ceiling on review rounds.
4. **What wins** when two rules collide.
5. **The creative direction goes to the person before the polish.**
6. **Where the deliveries go**.
7. **The phases** are described in the file, each with its own end point.

## 9. Measure it

Run `python tools/analyze_usage.py --session <id> --session <id>` on the sessions of a run. Look at:
the share of cache reads, what started the turns, the busiest hour of idle `/goal` re-runs, and the
scenarios. If idle re-runs or messages from other sessions dominate, tighten the rules above before you
change the model.
