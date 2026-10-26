#!/usr/bin/env python3
"""Split the test files of a suite into N parts of equal DURATION, so N parallel processes finish together.

Splitting by file name gives parts that take very different times (we saw 523 s to 1,045 s on 306 files);
the suite then lasts as long as the slowest part. This tool uses the time each file really took, measured
once from a JUnit XML report, and balances the parts with the LPT rule (longest processing time first).

Two steps:

  1. timings: read one or more JUnit XML files (pytest --junitxml) and write the seconds per test file.
       python tools/split_tests.py timings part1.xml part2.xml --machine laptop --out timings.json

     The file of a test is the `file` attribute of its <testcase>. When it is missing, it is taken from
     `classname`: the dots become `/`, `.py` is added, and the trailing components that start with an
     uppercase letter (test classes, also nested ones) are dropped. So `tests.test_x.TestX` is
     `tests/test_x.py`. A module whose name starts with an uppercase letter needs the `file` attribute.
     The same test (file, classname, name) found in two reports counts once, with its larger time.

  2. plan: list the files of one part, or a summary of all the parts.
       python tools/split_tests.py plan --glob 'tests/test_*.py' --timings timings.json --parts 6 --part 3
       pytest $(python tools/split_tests.py plan --glob 'tests/test_*.py' --timings timings.json --parts 6 --part 3)

     A file without a time weighs the median of the known times. With no times at all the files are
     split by name (round robin) and a warning goes to stderr. Without --part the output is a summary:
     files and estimated seconds per part, the longest part, and the same estimate for a split by name.

Usage:
  python tools/split_tests.py timings JUNIT [JUNIT ...] [--machine LABEL] [--out timings.json]
  python tools/split_tests.py plan (--files-from LIST.txt | --glob PATTERN) --timings timings.json --parts N
                                   [--part I] [--json] [--max-missing 20]

Exit codes: 0 ok, 2 wrong arguments or unreadable input.

Only the standard library is needed (Python 3.10 or newer).
"""
import argparse
import glob
import json
import statistics
import sys
import xml.etree.ElementTree as ET

DEFAULT_MAX_MISSING = 20    # more files than this without a time: the timings are stale, measure again


# --- timings ---

def norm_path(path):
    """A path with forward slashes and no leading ./, the same on every system."""
    path = path.replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    return path


def file_from_classname(classname):
    """tests.test_x.TestX -> tests/test_x.py (see the rule in the module docstring)."""
    parts = classname.split('.')
    while len(parts) > 1 and parts[-1][:1].isupper():
        parts.pop()
    return '/'.join(parts) + '.py'


def read_junit(paths):
    """Seconds per test file, summed over the test cases of every JUnit file in paths."""
    per_test = {}   # (file, classname, name) -> seconds; the same test in two reports counts once
    for path in paths:
        for case in ET.parse(path).getroot().iter('testcase'):
            classname = case.get('classname') or ''
            name = case.get('name') or ''
            file = norm_path(case.get('file') or file_from_classname(classname))
            try:
                seconds = float(case.get('time') or 0)
            except ValueError:
                seconds = 0.0
            key = (file, classname, name)
            per_test[key] = max(per_test.get(key, 0.0), seconds)
    files = {}
    for (file, _, _), seconds in per_test.items():
        files[file] = files.get(file, 0.0) + seconds
    return {file: round(seconds, 3) for file, seconds in files.items()}


# --- splitting ---

def round_robin(files, parts):
    """The split by name: the sorted files dealt one by one to each part."""
    ordered = sorted(files)
    return [ordered[i::parts] for i in range(parts)]


def weights_for(files, timings):
    """(weight per file, files without a time). A file without a time weighs the median of the known ones."""
    known = [timings[f] for f in files if f in timings]
    median = statistics.median(known) if known else 0.0
    weights = {f: timings.get(f, median) for f in files}
    missing = sorted(f for f in files if f not in timings)
    return weights, missing


def plan(files, timings, parts):
    """LPT split. Returns (files of each part, estimated seconds of each part, files without a time)."""
    files = sorted(set(files))
    weights, missing = weights_for(files, timings)
    if not any(f in timings for f in files):
        return round_robin(files, parts), [0.0] * parts, missing
    groups = [[] for _ in range(parts)]
    loads = [0.0] * parts
    for f in sorted(files, key=lambda f: (-weights[f], f)):
        i = min(range(parts), key=lambda k: (loads[k], k))   # the least loaded part, the lowest index on a tie
        groups[i].append(f)
        loads[i] += weights[f]
    return [sorted(g) for g in groups], loads, missing


# --- command line ---

def load_timings(parser, path):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return {norm_path(k): float(v) for k, v in data['files'].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        parser.error('cannot read timings from %s: %s' % (path, e))


def load_files(parser, args):
    if args.files_from:
        try:
            with open(args.files_from, encoding='utf-8') as f:
                found = [norm_path(line.strip()) for line in f if line.strip()]
        except OSError as e:
            parser.error('cannot read %s: %s' % (args.files_from, e))
    else:
        found = [norm_path(p) for p in glob.glob(args.glob)]
    if not found:
        parser.error('no test files found')
    return sorted(set(found))


def cmd_timings(parser, args):
    try:
        files = read_junit(args.junit)
    except (OSError, ET.ParseError) as e:
        parser.error('cannot read the JUnit file: %s' % e)
    text = json.dumps({'machine': args.machine, 'files': dict(sorted(files.items()))}, indent=2)
    if args.out:
        with open(args.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text + '\n')
    else:
        print(text)


def cmd_plan(parser, args):
    if args.parts < 1:
        parser.error('--parts must be at least 1')
    if args.part is not None and not 1 <= args.part <= args.parts:
        parser.error('--part must be between 1 and %d (got %d)' % (args.parts, args.part))
    files = load_files(parser, args)
    timings = load_timings(parser, args.timings)
    groups, loads, missing = plan(files, timings, args.parts)

    if len(missing) == len(files):
        print('warning: no timings for any of the %d files: splitting by name' % len(files), file=sys.stderr)
    elif len(missing) > args.max_missing:
        print('warning: %d file(s) have no timing (more than --max-missing %d): they weigh the median, '
              'measure the timings again' % (len(missing), args.max_missing), file=sys.stderr)

    if args.part is not None:
        chosen = groups[args.part - 1]
        if args.json:
            print(json.dumps({'part': args.part, 'files': chosen}, indent=2))
        else:
            for f in chosen:
                print(f)
        return

    weights, _ = weights_for(files, timings)
    by_name = [sum(weights[f] for f in g) for g in round_robin(files, args.parts)]
    longest = max(loads)
    by_name_longest = max(by_name)
    if args.json:
        print(json.dumps({
            'parts': [{'index': i + 1, 'files': g, 'seconds': round(s, 1)} for i, (g, s) in enumerate(zip(groups, loads))],
            'longest': round(longest, 1),
            'round_robin_longest': round(by_name_longest, 1),
            'missing': missing,
        }, indent=2))
        return
    print('%d files in %d parts' % (len(files), args.parts))
    for i, (g, s) in enumerate(zip(groups, loads)):
        print('part %d: %d files, %.1f s' % (i + 1, len(g), s))
    if len(missing) == len(files):
        print('no timings: the parts are split by name, seconds are unknown')
        return
    print('longest part: %.1f s (part %d)' % (longest, loads.index(longest) + 1))
    gain = (by_name_longest - longest) / by_name_longest * 100 if by_name_longest else 0.0
    print('round robin by name, same weights: longest part %.1f s (this plan is %.0f%% shorter)'
          % (by_name_longest, gain))


def build_parser():
    parser = argparse.ArgumentParser(description='Split test files into parts of equal duration.')
    sub = parser.add_subparsers(dest='command', required=True)

    t = sub.add_parser('timings', help='seconds per test file, from JUnit XML reports')
    t.add_argument('junit', nargs='+', help='JUnit XML files (pytest --junitxml); their times add up')
    t.add_argument('--machine', help='a label for the machine that measured the times')
    t.add_argument('--out', help='write the JSON here (default: stdout)')
    t.set_defaults(func=cmd_timings)

    p = sub.add_parser('plan', help='the files of one part, or a summary of all the parts')
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--files-from', help='text file with one test file per line')
    source.add_argument('--glob', help="pattern relative to the current directory, e.g. 'tests/test_*.py'")
    p.add_argument('--timings', required=True, help='JSON written by the timings command')
    p.add_argument('--parts', type=int, required=True, help='number of parts (parallel processes)')
    p.add_argument('--part', type=int, help='print only the files of this part (1-based), one per line')
    p.add_argument('--json', action='store_true', help='JSON output')
    p.add_argument('--max-missing', type=int, default=DEFAULT_MAX_MISSING,
                   help='warn when more files than this have no timing (default %(default)s)')
    p.set_defaults(func=cmd_plan)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(parser, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
