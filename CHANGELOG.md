# Changelog

## v0.2 — 2026-10-26

What a second, longer run taught us: 63 hours, 7 sessions, 28 subagents, 9,930 turns, on Claude Code
2.1.310. The full account is in [docs/run-63h.md](docs/run-63h.md).

### New tools

- **`tools/run_report.py`**: where the *time* of a run went. Active hours, turns, compactions (the
  context they fired at, the summary, the context of the first reply after it, the room left between
  the two), minutes spent compacting, tool errors by kind with the avoidable ones counted apart,
  messages between sessions, gaps of an hour or more. Times in UTC. `--out` and `--json` for a report
  file.
- **`tools/split_tests.py`**: splits a test suite into parts of equal *duration*, from the times in a
  JUnit XML report (longest first, files without a time get the median). On our suite: 17 min 37 s down
  to 13 min 52 s (-21%).

### Changed

- **Compaction window 333,000** in `hooks/settings.example.json` (was 400,000): compaction near 300k,
  hook reminders at 233k and 273k. A reviewer that mostly reads can keep 400,000.
- **The restart is about 145k, not 52k.** The summary is about 52k, but the first call after a
  compaction already carries about 145k, because Claude Code attaches the recently read files and other
  context again. At a 188k threshold that leaves about 43k of room. The charts and the estimates use
  the measured restart.

### New docs and templates

- [docs/run-63h.md](docs/run-63h.md): the run, where the time went, 13 levers with what we measured,
  what we did and the result.
- [docs/heavy-jobs.md](docs/heavy-jobs.md): many sessions on one machine. One queue for heavy jobs,
  a memory cap per job, more processes instead of more threads, a watchdog that stops only its own
  tree, the night work in one script, an independent review next to the tests.
- [docs/settings.md](docs/settings.md): the `compact_boundary` metadata, and how to choose the window.
- [docs/multi-session.md](docs/multi-session.md): go-aheads in batches, decisions in one message, the
  night script.
- `templates/CLAUDE.working-rules.md`: five working rules against the avoidable tool errors.
- New README and charts in `docs/img/`.

## v0.1 — 2026-10-19

First release: the memory hook (`hooks/memory_hook.py`), `tools/analyze_usage.py`, the multi-session
guide, the state file and compact-instructions templates. Measured on a 25-hour run on Claude Code
2.1.302.
