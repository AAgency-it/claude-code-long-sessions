## Working rules for long sessions

<!-- Paste this section into your CLAUDE.md. Each rule answers a kind of tool error that cost us time in a
     63-hour run (docs/run-63h.md): together they were 189 of 385 failed calls. Adapt the wording. -->

- **After a compaction, read a file again before you edit it.** The summary remembers the file; the edit
  tool does not. This was the most common error of the run (115 of 385).
- **Scripts longer than about 20 lines go into a file**, written with the Write tool and then run by name.
  Pasted into one command line they break on the first quote, and a hook may block them.
- **Long output goes into a file**: test runs, logs, listings, long pages read in the browser. Only the
  summary enters the context, and from the browser ask for short JSON.
- **Secrets never appear in a command, a prompt or a log.** Pass them through a wrapper that injects them
  into the environment of the command it runs.
- **Take the time from the clock** (`date`), never from memory: sessions guess wrong by an hour or more,
  and the wrong time ends up in reports and comments.
