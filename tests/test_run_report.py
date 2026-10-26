"""Tests for tools/run_report.py, on synthetic transcripts (tests/fixtures/run).

Usage, from the repository root: py -3 -m unittest discover -s tests -v

The main fixture, sess-run-1, is one session of 2026-01-20 with a known shape:
  - 10 model turns (a3 is streamed on two lines, so it counts once), 8 tool errors out of 10 tool results;
  - a gap of 330 s (not active) and a gap of 4800 s (not active, listed);
  - two compactions (preTokens 300000 and 350000), restarting at 45000 and 60000 tokens of context;
  - one SendMessage call;
  - a subagent (2 turns, 30 s of activity) that must stay out of the main numbers.
sess-run-2 is a tiny session of 2026-01-25.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, 'tools', 'run_report.py')
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import run_report as rr  # noqa: E402

RUN = os.path.join(HERE, 'fixtures', 'run')
ONE = 'sess-run-1'
TWO = 'sess-run-2'


def run_main(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rr.main(['--projects-dir', RUN, *args])
    return code, buf.getvalue()


def run_main_in(projects_dir, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rr.main(['--projects-dir', projects_dir, *args])
    return code, buf.getvalue()


def run_json(*args):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, 'report.json')
        code, text = run_main('--out', out, *args)
        with open(out, encoding='utf-8') as fh:
            return code, text, json.load(fh)


class ActiveTime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, cls.rep = run_json()
        cls.s = cls.rep['sessions'][ONE]

    def test_window_in_calendar_hours(self):
        self.assertAlmostEqual(self.s['hours'], 5460 / 3600, places=3)

    def test_active_time_skips_gaps_over_the_active_limit(self):
        # 150 s + 160 s + 20 s of intervals up to 300 s; the 330 s and 4800 s gaps are left out
        self.assertAlmostEqual(self.s['active_hours'], 330 / 3600, places=4)

    def test_active_limit_is_an_option(self):
        _, _, rep = run_json('--active-gap', '400')
        self.assertAlmostEqual(rep['sessions'][ONE]['active_hours'], (330 + 330) / 3600, places=4)

    def test_long_gaps_are_listed_with_start_end_and_minutes(self):
        gaps = self.s['gaps']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['start'], '2026-01-20T10:10:40+00:00')
        self.assertEqual(gaps[0]['end'], '2026-01-20T11:30:40+00:00')
        self.assertAlmostEqual(gaps[0]['minutes'], 80.0, places=2)

    def test_gap_report_limit_is_an_option(self):
        _, text, rep = run_json('--gap-report', '300')
        self.assertEqual([round(g['minutes']) for g in rep['sessions'][ONE]['gaps']], [6, 80])
        self.assertIn('80', text)

    def test_seconds_per_turn(self):
        self.assertAlmostEqual(self.s['seconds_per_turn'], 33.0, places=2)


class Turns(unittest.TestCase):
    def test_a_message_id_on_several_records_is_one_turn(self):
        _, _, rep = run_json()
        self.assertEqual(rep['sessions'][ONE]['turns'], 10)

    def test_the_raw_reader_counts_the_same_turns(self):
        raw = rr.analyze_transcript(os.path.join(RUN, 'proj-r', 'sess-run-1.jsonl'))
        self.assertEqual(raw['turns'], 10)


class Compactions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, cls.rep = run_json()
        cls.c = cls.rep['sessions'][ONE]['compactions']

    def test_count_and_median_threshold(self):
        self.assertEqual(self.c['count'], 2)
        self.assertEqual(self.c['median_pre_tokens'], 325000)

    def test_restart_context_is_the_first_reply_after_the_boundary(self):
        self.assertEqual(self.c['restart_contexts'], [45000, 60000])
        self.assertEqual(self.c['restart_median'], 52500)

    def test_summary_size_is_the_median_of_post_tokens(self):
        self.assertEqual(self.c['summary_median'], 52000)

    def test_room_is_what_is_left_between_a_restart_and_the_next_compaction(self):
        # (300000 - 45000) and (350000 - 60000)
        self.assertEqual(self.c['room_median'], 272500)

    def test_without_post_tokens_the_summary_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'sess-x.jsonl'), 'w', encoding='utf-8') as fh:
                fh.write(json.dumps({'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-01-10T10:00:00Z',
                                     'compactMetadata': {'trigger': 'auto', 'preTokens': 300000}}) + '\n')
                fh.write(json.dumps({'type': 'assistant', 'timestamp': '2026-01-10T10:00:10Z',
                                     'message': {'id': 'x1', 'content': [], 'usage': {'input_tokens': 100000}}}) + '\n')
            _, text = run_main_in(d, '--json')
        c = json.loads(text)['sessions']['sess-x']['compactions']
        self.assertIsNone(c['summary_median'])
        self.assertEqual(c['room_median'], 200000)

    def test_duration_is_reported_in_minutes(self):
        self.assertAlmostEqual(self.c['mean_minutes'], 1.5, places=3)
        self.assertAlmostEqual(self.c['total_minutes'], 3.0, places=3)

    def test_a_compaction_every_n_turns(self):
        self.assertAlmostEqual(self.c['turns_per_compaction'], 5.0, places=3)

    def test_a_session_without_compactions(self):
        c = self.rep['sessions'][TWO]['compactions']
        self.assertEqual(c['count'], 0)
        self.assertIsNone(c['median_pre_tokens'])
        self.assertIsNone(c['turns_per_compaction'])


class ErrorClassification(unittest.TestCase):
    def test_each_category(self):
        cases = [
            ('File has not been read yet. Read it first.', 'Edit', 'edit before read'),
            ('File has been modified since read, either by the user.', 'Edit', 'file changed since read'),
            ('PreToolUse:Bash hook error: blocked', 'Bash', 'blocked by a hook'),
            ('Some text with a hook error inside', 'Bash', 'blocked by a hook'),
            ('Exit code 1\nboom', 'Bash', 'command exited with an error'),
            ('String to replace not found in file.', 'Edit', 'string to replace not found'),
            ("The user doesn't want to proceed with this tool use.", 'Write', 'denied by permissions'),
            ('Permission to use Bash has been denied.', 'Bash', 'denied by permissions'),
            ('Navigation failed', 'mcp__claude-in-chrome__navigate', 'browser'),
            ('Timeout', 'mcp__some-browser__click', 'browser'),
            ('Something odd', 'Bash', 'other'),
            ('<tool_use_error>File has not been read yet. Read it first.</tool_use_error>', 'Edit', 'edit before read'),
            ('<tool_use_error>File has been modified since read, by a linter.</tool_use_error>', 'Edit',
             'file changed since read'),
            ('<tool_use_error>String to replace not found in file.</tool_use_error>', 'Edit',
             'string to replace not found'),
        ]
        for text, tool, expected in cases:
            self.assertEqual(rr.classify_error(text, tool), expected, text)

    def test_only_three_categories_are_avoidable(self):
        self.assertEqual(rr.AVOIDABLE, {'edit before read', 'file changed since read', 'blocked by a hook'})

    def test_totals_and_shares_on_the_fixture(self):
        _, _, rep = run_json()
        e = rep['sessions'][ONE]['tool_errors']
        self.assertEqual(e['total'], 8)
        self.assertEqual(e['tool_results'], 10)
        self.assertAlmostEqual(e['share_of_results'], 0.8, places=6)
        self.assertEqual(e['avoidable'], 3)
        self.assertAlmostEqual(e['avoidable_share'], 3 / 8, places=6)
        self.assertEqual(e['by_category'], {
            'edit before read': 1, 'file changed since read': 1, 'blocked by a hook': 1,
            'command exited with an error': 1, 'string to replace not found': 1,
            'denied by permissions': 1, 'browser': 1, 'other': 1})
        self.assertEqual(e['by_tool']['Edit'], 3)

    def test_the_tool_name_comes_from_the_matching_tool_use(self):
        _, _, rep = run_json()
        self.assertEqual(rep['sessions'][ONE]['tool_errors']['by_tool']['mcp__claude-in-chrome__navigate'], 1)


class NormalisedLines(unittest.TestCase):
    def test_paths_and_numbers_are_replaced(self):
        self.assertEqual(rr.normalise_line('Exit code 1\nTraceback in /home/x/app.py'), 'Exit code N')
        self.assertEqual(rr.normalise_line('Cannot open C:\\Users\\Someone\\a b.txt line 42'),
                         'Cannot open <path> b.txt line N')
        self.assertEqual(rr.normalise_line('see ~/notes/todo.md and ./x/y.py, ../z'), 'see <path> and <path>, <path>')

    def test_tags_are_removed_before_the_line_is_taken(self):
        self.assertEqual(rr.normalise_line('<tool_use_error>File has not been read yet.</tool_use_error>'),
                         'File has not been read yet.')
        self.assertEqual(rr.normalise_line('<tool_use_error>\nExit code 3\n</tool_use_error>'), 'Exit code N')

    def test_text_that_is_not_a_path_is_kept(self):
        self.assertEqual(rr.normalise_line('use and/or here'), 'use and/or here')

    def test_only_the_first_non_empty_line_and_at_most_90_characters(self):
        self.assertEqual(rr.normalise_line('\n\n  first line  \nsecond'), 'first line')
        self.assertEqual(len(rr.normalise_line('x' * 300)), 90)

    def test_report_has_normalised_lines_and_never_the_rest_of_the_text(self):
        _, text, rep = run_json()
        lines = {x['line']: x['count'] for x in rep['sessions'][ONE]['tool_errors']['top_lines']}
        self.assertEqual(lines['Exit code N'], 1)
        self.assertLessEqual(len(rep['sessions'][ONE]['tool_errors']['top_lines']), 10)
        self.assertIn('File has not been read yet. Read it first before writing to it.', lines)
        self.assertNotIn('tool_use_error', text)
        self.assertNotIn('Traceback', text)
        self.assertNotIn('/home/someone', json.dumps(rep))


class Messages(unittest.TestCase):
    def test_send_message_calls_are_counted(self):
        _, _, rep = run_json()
        self.assertEqual(rep['sessions'][ONE]['send_messages'], 1)
        self.assertEqual(rep['sessions'][TWO]['send_messages'], 0)
        self.assertEqual(rep['totals']['send_messages'], 1)


class Subagents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.text, cls.rep = run_json()

    def test_they_are_summarised_apart(self):
        sub = self.rep['sessions'][ONE]['subagents']
        self.assertEqual(sub['count'], 1)
        self.assertEqual(sub['turns'], 2)
        self.assertAlmostEqual(sub['active_hours'], 30 / 3600, places=5)

    def test_they_do_not_enter_the_main_totals(self):
        t = self.rep['totals']
        self.assertEqual(t['turns'], 11)                       # 10 + 1, no subagent turns
        self.assertEqual(self.rep['sessions'][ONE]['tool_errors']['total'], 8)   # the subagent had an error too
        self.assertEqual(t['tool_errors']['total'], 8)
        self.assertEqual(t['subagents']['count'], 1)
        self.assertEqual(t['subagents']['turns'], 2)

    def test_the_text_has_a_subagent_section(self):
        self.assertIn('Subagents', self.text)


class Totals(unittest.TestCase):
    def test_total_row_and_run_window(self):
        _, text, rep = run_json()
        t = rep['totals']
        self.assertEqual(t['sessions'], 2)
        self.assertEqual(t['turns'], 11)
        self.assertEqual(t['compactions']['count'], 2)
        self.assertAlmostEqual(t['active_hours'], (330 + 20) / 3600, places=4)
        self.assertAlmostEqual(t['seconds_per_turn'], 350 / 11, places=2)
        self.assertIn('TOTAL', text)

    def test_the_run_window_spans_all_the_sessions(self):
        _, _, rep = run_json()
        w = rep['totals']['window']
        self.assertEqual(w['first'], '2026-01-20T10:00:00+00:00')
        self.assertEqual(w['last'], '2026-01-25T10:00:20+00:00')

    def test_parallel_sessions_are_not_counted_twice_in_the_run_active_time(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('sess-p1', 'sess-p2'):
                with open(os.path.join(d, name + '.jsonl'), 'w', encoding='utf-8') as fh:
                    for sec, mid in ((0, 'm1'), (60, 'm2'), (120, 'm3')):
                        fh.write(json.dumps({'type': 'assistant', 'timestamp': f'2026-01-10T10:0{sec // 60}:00Z',
                                             'message': {'id': name + mid, 'content': [], 'usage': {}}}) + '\n')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out = os.path.join(d, 'r.json')
                rr.main(['--projects-dir', d, '--out', out])
            with open(out, encoding='utf-8') as fh:
                rep = json.load(fh)
        self.assertAlmostEqual(rep['totals']['active_hours'], 120 / 3600, places=4)       # the union
        self.assertAlmostEqual(rep['totals']['sessions_active_hours'], 240 / 3600, places=4)   # the sum


class Filters(unittest.TestCase):
    def test_session_option(self):
        _, _, rep = run_json('--session', TWO)
        self.assertEqual(list(rep['sessions']), [TWO])

    def test_since_and_until_select_sessions(self):
        _, _, rep = run_json('--since', '2026-01-21')
        self.assertEqual(list(rep['sessions']), [TWO])
        _, _, rep = run_json('--until', '2026-01-20')          # a plain date includes the whole day
        self.assertEqual(list(rep['sessions']), [ONE])

    def test_since_cuts_inside_a_session(self):
        _, _, rep = run_json('--since', '2026-01-20T11:00:00Z', '--until', '2026-01-20')
        s = rep['sessions'][ONE]
        self.assertEqual(s['turns'], 1)
        self.assertEqual(s['compactions']['count'], 1)
        self.assertEqual(s['compactions']['median_pre_tokens'], 350000)
        self.assertEqual(s['tool_errors']['total'], 0)
        self.assertEqual(s['subagents']['count'], 0)
        self.assertAlmostEqual(s['hours'], 20 / 3600, places=4)

    def test_no_sessions_found(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rr.main(['--projects-dir', os.path.join(RUN, 'nope')])
        self.assertEqual(code, 1)
        self.assertIn('No sessions', buf.getvalue())

    def test_nothing_in_the_time_range(self):
        code, text = run_main('--since', '2030-01-01')
        self.assertEqual(code, 1)
        self.assertIn('No records', text)

    def test_bad_date_is_a_clear_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            rr.main(['--projects-dir', RUN, '--since', 'yesterday-ish'])


class Table(unittest.TestCase):
    def test_columns_and_sections(self):
        _, text = run_main()
        header = next(l for l in text.splitlines() if l.startswith('session'))
        for column in ('hours', 'active h', 'turns', 's/turn', 'compactions', 'compact at', 'summary', 'restart at',
                       'room', 'compaction min', 'tool errors', 'avoidable'):
            self.assertIn(column, header)
        self.assertIn(ONE, text)
        self.assertIn('Tool errors', text)
        self.assertIn('Gaps of', text)
        self.assertIn('325k', text)                     # median preTokens
        self.assertIn('52k', text)                      # median postTokens (the summary)
        self.assertIn('273k', text)                     # median room
        self.assertIn('edit before read', text)

    def test_times_are_marked_as_utc(self):
        _, text = run_main()
        self.assertIn('2026-01-20T10:10Z -> 2026-01-20T11:30Z', text)
        self.assertIn('UTC', text)

    def test_no_money_in_the_output(self):
        _, text = run_main()
        self.assertNotIn('$', text)
        self.assertNotIn('cost', text.lower())

    def test_json_option_prints_only_json(self):
        _, text = run_main('--json')
        rep = json.loads(text)
        self.assertEqual(sorted(rep['sessions']), [ONE, TWO])


class Command(unittest.TestCase):
    def test_runs_as_a_script_and_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'report.json')
            r = subprocess.run([sys.executable, TOOL, '--projects-dir', RUN, '--out', out],
                               capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(ONE.encode(), r.stdout)
            with open(out, encoding='utf-8') as fh:
                rep = json.load(fh)
        self.assertEqual(rep['totals']['turns'], 11)

    def test_the_transcripts_are_not_modified(self):
        path = os.path.join(RUN, 'proj-r', 'sess-run-1.jsonl')
        before = os.stat(path).st_mtime_ns
        run_main()
        self.assertEqual(os.stat(path).st_mtime_ns, before)


if __name__ == '__main__':
    unittest.main()
