---
type: session-state
session: <session name>
updated: <YYYY-MM-DD HH:MM>
---

<!-- The state file of ONE long session. REWRITE it at every important step, do not let it grow:
     8,000 characters at most. After every compaction the memory hook puts it back into the context,
     read from disk, so it must be enough to resume the work.
     Only its own session writes it. Name: <state folder>/<session-name>.md
     (the state folder is MEMORY_STATE_DIR, default ~/.claude/session-state).
     Copy this template to <state folder>/_TEMPLATE.md: the hook points the model to it. -->

# Goal and phase
<one sentence on the goal; the phase in progress and the ones that remain>

# Done
- <what, with commits, branches, file paths and real numbers>

# Decisions
- <YYYY-MM-DD, who (the person, the reviewer, me), what, in the exact words if they matter>

# Waiting on
- <who, what, by when, where I asked>

# Next step
<the exact action to resume from, with the command or the file>

# Re-read after compaction
- <paths of the handoffs, plans and files the next step needs>
