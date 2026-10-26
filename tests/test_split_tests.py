"""Tests for tools/split_tests.py, on small synthetic JUnit files (tests/fixtures/split).

Usage, from the repository root: py -3 -m unittest discover -s tests -v
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
TOOL = os.path.join(ROOT, 'tools', 'split_tests.py')
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import split_tests as st  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures', 'split')
JUNIT_A = os.path.join(FIXTURES, 'junit_a.xml')
JUNIT_B = os.path.join(FIXTURES, 'junit_b.xml')
FILES_TXT = os.path.join(FIXTURES, 'files.txt')


def run_tool(*args, cwd=None):
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, cwd=cwd)


def write_json(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return path


class Timings(unittest.TestCase):
    def test_seconds_are_summed_per_file(self):
        files = st.read_junit([JUNIT_A])
        self.assertEqual(files['tests/test_alpha.py'], 6.0)

    def test_same_test_in_two_junits_counts_once_with_the_max(self):
        files = st.read_junit([JUNIT_A, JUNIT_B])
        # test_dup: 1.5 s in one junit, 2.0 s in the other -> 2.0, plus test_other 1.0
        self.assertEqual(files['tests/test_gamma.py'], 3.0)

    def test_file_comes_from_classname_when_the_attribute_is_missing(self):
        self.assertEqual(st.file_from_classname('tests.test_gamma'), 'tests/test_gamma.py')
        self.assertEqual(st.file_from_classname('tests.test_beta.TestBeta'), 'tests/test_beta.py')
        self.assertEqual(st.file_from_classname('tests.sub.test_x.TestOuter.TestInner'), 'tests/sub/test_x.py')

    def test_file_attribute_wins_and_is_normalised_to_forward_slashes(self):
        files = st.read_junit([JUNIT_B])
        self.assertEqual(files['tests/sub/test_delta.py'], 0.5)
        self.assertNotIn('tests\\sub\\test_delta.py', files)

    def test_timings_command_writes_json_with_machine_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'timings.json')
            r = run_tool('timings', JUNIT_A, JUNIT_B, '--machine', 'laptop', '--out', out)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(out, encoding='utf-8') as f:
                data = json.load(f)
        self.assertEqual(data['machine'], 'laptop')
        self.assertEqual(data['files'], {'tests/test_alpha.py': 6.0, 'tests/test_beta.py': 3.0,
                                         'tests/test_gamma.py': 3.0, 'tests/sub/test_delta.py': 0.5})

    def test_timings_command_prints_json_to_stdout_by_default(self):
        r = run_tool('timings', JUNIT_A)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertIsNone(data['machine'])
        self.assertEqual(data['files']['tests/test_beta.py'], 3.0)


class Splitting(unittest.TestCase):
    def test_lpt_balances_a_known_case(self):
        timings = {'a': 7, 'b': 6, 'c': 5, 'd': 4}
        parts, loads, missing = st.plan(list(timings), timings, 2)
        self.assertEqual(sorted(loads), [11, 11])
        self.assertEqual(missing, [])

    def test_lpt_is_greedy_and_ties_go_to_the_lowest_part(self):
        timings = {'a': 8, 'b': 7, 'c': 6, 'd': 5, 'e': 4}
        parts, loads, _ = st.plan(list(timings), timings, 2)
        self.assertEqual(loads, [17, 13])
        self.assertEqual(parts, [['a', 'd', 'e'], ['b', 'c']])

    def test_equal_times_are_ordered_by_name(self):
        timings = {'b': 1, 'a': 1, 'c': 1}
        parts, _, _ = st.plan(['c', 'b', 'a'], timings, 3)
        self.assertEqual(parts, [['a'], ['b'], ['c']])

    def test_file_without_a_time_weighs_the_median_of_the_known_ones(self):
        timings = {'a': 10, 'b': 2, 'c': 4}
        parts, loads, missing = st.plan(['a', 'b', 'c', 'new'], timings, 2)
        self.assertEqual(missing, ['new'])
        # median of 10, 2, 4 is 4: 'new' weighs 4, so the loads are a=10 | c+new+b=10
        self.assertEqual(sorted(loads), [10, 10])

    def test_no_times_at_all_falls_back_to_round_robin_by_name(self):
        parts, loads, missing = st.plan(['d', 'b', 'a', 'c'], {}, 2)
        self.assertEqual(parts, [['a', 'c'], ['b', 'd']])
        self.assertEqual(missing, ['a', 'b', 'c', 'd'])

    def test_timings_of_files_not_in_the_list_are_ignored(self):
        parts, loads, _ = st.plan(['a'], {'a': 1, 'gone': 100}, 2)
        self.assertEqual(parts, [['a'], []])
        self.assertEqual(loads, [1, 0])


class PlanCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.timings = write_json(self.tmp.name, 'timings.json', {'machine': None, 'files': {
            'tests/test_alpha.py': 6.0, 'tests/test_beta.py': 3.0,
            'tests/test_gamma.py': 3.0, 'tests/sub/test_delta.py': 0.5}})

    def plan_args(self, *extra):
        return ('plan', '--files-from', FILES_TXT, '--timings', self.timings, *extra)

    def test_part_prints_only_its_files_one_per_line(self):
        r = run_tool(*self.plan_args('--parts', '2', '--part', '1'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ['tests/test_alpha.py', 'tests/test_gamma.py'])

    def test_union_of_the_parts_is_every_file_without_duplicates(self):
        seen = []
        for i in (1, 2, 3):
            r = run_tool(*self.plan_args('--parts', '3', '--part', str(i)))
            self.assertEqual(r.returncode, 0, r.stderr)
            seen += r.stdout.split()
        with open(FILES_TXT, encoding='utf-8') as f:
            expected = f.read().split()
        self.assertEqual(sorted(seen), sorted(expected))
        self.assertEqual(len(seen), len(set(seen)))

    def test_two_runs_give_the_same_output(self):
        first = run_tool(*self.plan_args('--parts', '2'))
        second = run_tool(*self.plan_args('--parts', '2'))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)

    def test_summary_shows_each_part_the_longest_and_the_round_robin_comparison(self):
        r = run_tool(*self.plan_args('--parts', '2'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('part 1:', r.stdout)
        self.assertIn('part 2:', r.stdout)
        self.assertIn('longest part', r.stdout)
        self.assertIn('round robin', r.stdout)

    def test_json_summary_has_the_parts_and_the_gain(self):
        r = run_tool(*self.plan_args('--parts', '2', '--json'))
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(len(data['parts']), 2)
        self.assertEqual(data['parts'][0]['index'], 1)
        self.assertLessEqual(data['longest'], data['round_robin_longest'])
        self.assertEqual(data['missing'], ['tests/test_epsilon.py'])

    def test_part_out_of_range_exits_2_with_a_clear_message(self):
        for bad in ('0', '3'):
            r = run_tool(*self.plan_args('--parts', '2', '--part', bad))
            self.assertEqual(r.returncode, 2, r.stdout)
            self.assertIn('--part must be between 1 and 2', r.stderr)

    def test_parts_below_one_exits_2(self):
        r = run_tool(*self.plan_args('--parts', '0'))
        self.assertEqual(r.returncode, 2)
        self.assertIn('--parts must be at least 1', r.stderr)

    def test_missing_timings_file_exits_2(self):
        r = run_tool('plan', '--files-from', FILES_TXT, '--timings', os.path.join(self.tmp.name, 'nope.json'),
                     '--parts', '2')
        self.assertEqual(r.returncode, 2)
        self.assertIn('nope.json', r.stderr)

    def test_no_timings_at_all_warns_and_splits_by_name(self):
        empty = write_json(self.tmp.name, 'empty.json', {'machine': None, 'files': {}})
        r = run_tool('plan', '--files-from', FILES_TXT, '--timings', empty, '--parts', '2', '--part', '1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('no timings', r.stderr)
        self.assertEqual(r.stdout.split(), ['tests/sub/test_delta.py', 'tests/test_beta.py',
                                            'tests/test_gamma.py'])

    def test_warns_when_files_without_a_time_exceed_the_threshold(self):
        r = run_tool(*self.plan_args('--parts', '2', '--max-missing', '0'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 file(s) have no timing', r.stderr)
        quiet = run_tool(*self.plan_args('--parts', '2'))
        self.assertNotIn('no timing', quiet.stderr)

    def test_glob_picks_the_files_relative_to_the_current_directory(self):
        work = os.path.join(self.tmp.name, 'work')
        os.makedirs(os.path.join(work, 'tests'))
        for name in ('test_one.py', 'test_two.py', 'helper.py'):
            open(os.path.join(work, 'tests', name), 'w').close()
        r = run_tool('plan', '--glob', 'tests/test_*.py', '--timings', self.timings, '--parts', '1', '--part', '1',
                     cwd=work)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ['tests/test_one.py', 'tests/test_two.py'])


if __name__ == '__main__':
    unittest.main()
