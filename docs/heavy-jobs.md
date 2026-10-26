# Heavy jobs on one machine

When several long sessions share one computer, the machine becomes the bottleneck before the model does.
In our 63-hour run ([run-63h.md](run-63h.md)) the sessions collided on memory 5 times, a watchdog killed
another session's test suite, and one script froze the computer by asking for 44.5 GB. This page is the
set of rules that fixed it. They are patterns, not a tool: the scripts we use are tied to our machine,
and each rule fits in a few lines of any shell.

## 1. One queue for the whole machine

Test suites, renders, builds and browser tests go through **one queue**, whatever session starts them.

- Each job takes a **ticket** (a file named with the time and its process id) in a shared folder.
- Only the **first live ticket** may start, and only when the machine has at least **3 GB of free
  memory** and at least **15 seconds** have passed since the previous start. The pause lets the job that
  just started claim its memory before the next one looks.
- A ticket whose process is dead is removed by whoever finds it. Store the process start time in the
  ticket: a process id can be reused by a new process.
- After **120 minutes** of waiting the job gives up with a distinct exit code (we use 75), so the session
  knows it was the queue and not the job.
- While it waits it prints one line a minute: how many jobs are ahead, how much memory is free. A session
  that sees "queued" waits; it does not start the job a second time.
- Light, short jobs may skip the queue with a flag.

## 2. A memory cap on every job

Every heavy job runs inside a container with a **memory cap**: a Job Object on Windows, a cgroup or
`systemd-run --scope -p MemoryMax=` on Linux, `ulimit -v` as a last resort. When the job and its children
go over the cap, their allocations fail and the job stops with an error, instead of filling the memory and
freezing every session on the machine. The cap is part of the command, so it is in the transcript and
the reviewer can see it.

Give heavy jobs a **low priority**, so the sessions themselves stay responsive.

## 3. More processes, not more threads

A Python or Node test process uses one core. To go faster, run **several processes in parallel**, each
on its own part of the suite. Do not pin them all to the same cores: our job wrapper did exactly that for
two days, and four parallel parts ran 4 to 5 times slower than they could.

- Split the suite by **measured time**, not by file name (`tools/split_tests.py`): the run takes as long as
  its longest part. On our suite: parts from 523-1,045 s became 677-832 s, and the whole suite went from
  17 min 37 s to 13 min 52 s.
- Budget memory per process (about 200 MB for one of our test processes) and keep 3 GB free.
- A part that fails **saves its whole output to a file**, so the cause can be read later instead of
  re-running the part.
- Keep **zero known failing tests**. A list of "reds we expect" is read by hand at every run, and it
  will be misread: we re-ran five parts for a result we had misread.

## 4. A watchdog stops only what it started

A watchdog that kills processes by name will kill another session's work. Ours did: a browser-test
watchdog stopped the whole test suite of a different session. A watchdog records the process tree it
started and stops only that tree.

## 5. Temp folders on a disk with room

Test temp folders and caches filled the system disk to 1.7 GB, and the browser stopped working. Point
`--basetemp`, build caches and render scratch folders at a disk with room, and clean them at the start of
each job.

## 6. The night in one script

A session waiting for a person does nothing at night. Whatever has to happen while you sleep goes into
**one script you start yourself before going to bed**; starting it is the go-ahead. Ours does, in order,
and stops at the first red:

1. checks that the commit to release is a fast-forward of the main branch;
2. prepares a clean working copy of that commit;
3. compiles with the Python version that production runs;
4. starts the full test suite in parallel parts and, **at the same moment**, an independent review of the
   diff by a separate model call;
5. reads the results: every part finished, no new reds, the review ends with an explicit green verdict;
6. only with an explicit `--release` flag: releases, waits for the deploy, and checks the result with
   read-only requests.

Without the flag it only says whether the commit is ready. It writes a short outcome file and a full log.
It also refuses to release a change to the frontend unless a screen test was declared done. Test the
script itself with fakes in place of the release, the suite and the review: one scenario per way it can
stop.

## 7. An independent review next to the tests

Tests check what someone thought of. A review by a **separate model call** with a clean context reads the
diff without the author's assumptions. In our run such reviews found a blocking syntax error that the
production Python version rejects, and read-only requests that were writing to the database, all with
every test green. Start the review **together** with the test suite, not after it: it costs minutes, not
hours, and the release waits for both anyway.

## 8. CI and cloud sessions

- **CI on every push** costs no tokens and no memory on your machine. We run the compile step on the
  production Python version and the fast tests on every push (about 4 minutes), and the full suite by
  hand. Two branches pushed at the same commit run CI twice.
- **A cloud session** can run a long suite away from your machine. It is the same model, so the changes are
  as good; in our pilot it was not faster (29 min 55 s, then 17 min 37 s on a second run of the same
  machine type) than six local processes (28-36 minutes). We use it to **free the machine's memory** when
  a render or a browser needs it. It has no access to your secrets, local disks or logged-in browser, so
  screen tests with real data stay local.
