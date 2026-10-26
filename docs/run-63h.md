# A 63-hour run, measured

Seven long sessions worked in parallel for 63 hours and shipped 12 software releases, 2 ad campaigns
and 9 video clips. They spent 5.3 of their 57.6 active hours compacting, and lost more time to errors
they already had rules against, to memory collisions on the machine and to one night spent waiting.
This page is what we measured, what we changed, and what the changes gave. Claude Code 2.1.310.

## The run

| | |
|---|---|
| Sessions | 7: six workers (two on the same backend, content production, two ad campaigns, an internal tool) and one reviewer |
| Subagents | 28, all on Sonnet |
| Calendar | 63 hours |
| Active session hours | 57.6 (gaps under 5 minutes count as work), one turn every 21 seconds |
| Turns | 9,930 in the sessions, 4,586 in the subagents |
| Tool calls | 10,770, of which 385 failed (3.6%) |
| Compactions | 327, 5.3 hours in total |
| Messages between sessions | 624 (318 to and from the reviewer) |
| Shipped | 12 releases of two web apps, 2 ad campaigns, 9 video clips; no data incidents |

Every release went through the same gate: tests green, an independent review, a script tried first in
dry-run, and a check after the release with read-only requests.

## Where the time went

### Compactions: the largest single cost

Six sessions were started by a launcher that set the compaction window to 220k, so they compacted at
about **188k** tokens and restarted from about **52k**. That is one compaction every 25 turns; the busiest
session compacted every 24 minutes. Each one took **58 seconds** on average.

The cost moved with it. In our first run (compaction near 967k) re-reading the cache was 78% of the
main-session cost. In this one, **writing the cache was 49%**: every restart rewrites it, then the
session reads its files again. The reviewer, which compacted near 367k, cost 5% less per turn than a
development session at 188k.

![Compactions by threshold](img/compaction-threshold.svg)

The estimate behind the chart: the context after a compaction stays at 52k, the average context sits at
83% of the cycle as measured, and each compaction costs 58 seconds plus about 25k tokens of re-reading.
At 300k the six sessions would have compacted 168 times instead of 306, saving about **2.2 hours**; each
turn re-reads more, so the token value goes up a little. We chose 300k (window 333k) for working
sessions and kept about 367k (window 400k) for the reviewer.

### Tool errors: half were avoidable

![Tool errors by kind](img/tool-errors.svg)

189 of the 385 failed calls broke a rule that was already written down:

- **115 "file has not been read yet"**, almost all right after a compaction: the model edits a file it
  remembers from the summary but no longer has in view.
- **42 long inline scripts** blocked by a hook that asks for scripts over a size limit to be written to a
  file first.
- **16 commands that named a secrets file**, blocked by a hook.
- **16 "file has been modified since read"**, mostly two sessions or a formatter touching the same file.

The fix is a habit, so it lives in `templates/CLAUDE.working-rules.md`: after a compaction, re-read a
file before editing it; long scripts go to a file; secrets go through a wrapper; long output goes to a
file and only its summary enters the context. Estimated saving: about **1.1 hours**.

### The machine

All sessions shared one PC with 16 GB of memory and 12 threads.

- **5 memory collisions** between sessions: a test suite, a video render and a browser test started
  together, and each collision meant a re-run.
- **A watchdog killed another session's test suite**: it stopped every process matching a name, not
  only the ones it had started.
- **One script asked for 44.5 GB** and froze the machine; we first blamed the heat.
- **A job wrapper pinned every heavy job to the same 2 threads** for two days: the suite ran 4 to 5 times
  slower than it could.
- **A nearly full system disk** (1.7 GB free, filled by test temp folders) blocked the browser.

What we changed is in [heavy-jobs.md](heavy-jobs.md). The test suite went from 185 minutes to 28-36
minutes on the same machine, with faster test fixtures and six processes instead of more threads.

### The night

From 01:30 to 07:52 no session was awake. A release had been ready since 01:26 and waited for a go-ahead
until 08:02. Wake-ups scheduled for 07:25 arrived late as well. A session that is waiting for a person
does nothing useful at night; a script started by that person before going to sleep does.

### Messages

624 messages between sessions, more than half to and from the reviewer. Each message wakes the receiver,
who re-reads its whole context. Most of them asked for or gave a go-ahead on a step that could be undone
anyway.

## The levers, one by one

Estimated savings are for a run of the same size. They overlap, so they do not add up exactly; together
about 7-8 session hours (13%) and 5 calendar hours.

![Estimated hours saved per lever](img/levers.svg)

| # | Lever | What we measured | What we did | Result |
|---|---|---|---|---|
| 1 | Compact near 300k in working sessions | 306 compactions at 188k, one every 25 turns | Window 333k in the launcher; hook reminders at 233k and 273k | In use; est. -2.2 h |
| 2 | Rules against the avoidable errors | 189 of 385 tool errors | Five working rules in `CLAUDE.md` | In use; est. -1.1 h |
| 3 | One queue for heavy jobs | 5 memory collisions, a suite killed | Machine-wide queue, memory cap per job, watchdog limited to its own tree | In use; est. -2.5 h, low confidence |
| 4 | Go-aheads in batches | 624 messages | The reviewer approves anything reversible at once; decisions for the person go in one message with choices | In use; est. -1.8 h |
| 5 | Night work in one script | A release waited 6.6 h for a go-ahead | One script: check, test, review, release, verify; it stops at the first red. The person starts it before sleeping | Ready, tried on 5 scenarios with fakes; est. -5 calendar h |
| 6 | Independent review next to the tests | API reviews found 1 blocker and several high issues with all tests green | A review by a separate model call starts together with the suite, not after it | In use; 16 review jobs used under 7% of the monthly API budget |
| 7 | "Ready" only after a screen test per role | Three bugs found only when the feature was switched on, all with green tests | A release that touches the frontend needs the screen test, with seed data shaped like production | In use |
| 8 | Compile on the production Python version | A syntax error passed on 3.12; production runs 3.11 | Compile step on 3.11 before every release and in CI | In use |
| 9 | No known failing tests | 4 known reds read by hand at every run; 5 parts re-run after a misread result | Tests fixed; red parts now save their full output | Done |
| 10 | Heavy output to files | One session wrote about 6,700 tokens to the cache per turn; another compacted every 3-5 minutes while reading a web dashboard | Logs and long reads go to a file; the browser returns short JSON | In use |
| 11 | Long suites in a cloud session | Local suite 28-36 minutes | Pilot: the same suite took 29 min 55 s in the cloud, and 17 min 37 s on a second run of the same machine | Not faster; we use it to free the machine's memory |
| 12 | Tests in CI on every push | No CI before | Compile on 3.11 plus the fast tests at every push, the full suite by hand | About 4 minutes per push, green on the first run |
| 13 | Parts of equal duration | Parts split by file name: 523 to 1,045 s | `tools/split_tests.py` | 17 min 37 s down to 13 min 52 s (-21%) |

## If you only do five things

1. **Set the compaction window on purpose.** Too high and every call re-reads a huge context; too low and
   you pay for restarts. Measure both with `tools/analyze_usage.py` and `tools/run_report.py`.
2. **Give every long session a state file** and the memory hook, so a compaction costs one re-read, not
   twenty turns of searching.
3. **Re-read before you edit after a compaction.** It was the most common error of the run.
4. **Put heavy jobs in one queue** with a memory cap, and let a watchdog stop only what it started.
5. **Let nobody wait at night.** Whatever has to run while you sleep goes into one script you start
   yourself, and stops at the first red.

## How the numbers were made

The transcripts of the seven sessions and their 28 subagents, read without changing them, over 72 hours
of local time. Turns are distinct model responses. Active hours are the sum of the gaps between records
shorter than 5 minutes. Token values use API list prices with the 1-hour cache write at 2x the input
price; they are the value of the work, not what was paid, since the sessions ran on a subscription.
`tools/run_report.py` reproduces the time figures on your own transcripts.
