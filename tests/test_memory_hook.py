"""Tests for hooks/memory_hook.py.

They run the real script the way Claude Code does: JSON on stdin, JSON on stdout.
Usage, from the repository root: py -3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), 'hooks', 'memory_hook.py')


def usage_line(total, sidechain=False, model='claude-opus-5-5'):
    """A transcript line for one model call whose context is `total` tokens."""
    return json.dumps({'type': 'assistant', 'isSidechain': sidechain, 'message': {
        'model': model, 'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 1000,
                                  'cache_read_input_tokens': total - 1002}}})


def tool_line(path, name='Write'):
    """A transcript line for one model call that used a tool on `path`."""
    return json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-5-5', 'content': [
        {'type': 'tool_use', 'id': 'toolu_1', 'name': name, 'input': {'file_path': path}}]}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='memory hook test ')      # with a space, like real paths
        self.state_dir = os.path.join(self.tmp, 'my notes', 'session-state')
        os.makedirs(self.state_dir)
        self.data = os.path.join(self.tmp, 'data')
        self.transcript = os.path.join(self.tmp, 'session.jsonl')
        self.env = dict(os.environ, MEMORY_STATE_DIR=self.state_dir, MEMORY_DATA_DIR=self.data,
                        CLAUDE_CODE_AUTO_COMPACT_WINDOW='400000')
        for name in ('MEMORY_THRESHOLD', 'MEMORY_FIRST_MARGIN', 'MEMORY_URGENT_MARGIN'):
            self.env.pop(name, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, command, payload, raw=None):
        stdin = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
        r = subprocess.run([sys.executable, SCRIPT, command], input=stdin.encode('utf-8'),
                           capture_output=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = os.path.join(self.data, 'errors.log')
        self.assertFalse(os.path.exists(log), open(log, encoding='utf-8').read() if os.path.exists(log) else '')
        out = r.stdout.decode('utf-8')
        return json.loads(out) if out.strip() else None

    def write_transcript(self, lines):
        with open(self.transcript, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')

    def state(self, name='release-plan.md', text='# Next step\ntry it\n', age_s=0):
        p = os.path.join(self.state_dir, name)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(text)
        if age_s:
            t = time.time() - age_s
            os.utime(p, (t, t))
        return p

    def session_data(self, sid='s1'):
        p = os.path.join(self.data, sid + '.json')
        if not os.path.exists(p):
            return {}
        with open(p, encoding='utf-8') as fh:
            return json.load(fh)

    def tool(self, name='Bash', file_path=None, sid='s1', **extra):
        payload = {'session_id': sid, 'transcript_path': self.transcript, 'hook_event_name': 'PostToolUse',
                   'tool_name': name, 'tool_input': {'file_path': file_path} if file_path else {'command': 'ls'}}
        payload.update(extra)
        return self.run_hook('after-tool', payload)


class Linking(Base):
    def test_writing_the_state_file_links_it(self):
        p = self.state()
        self.assertIsNone(self.tool('Write', p))
        self.assertEqual(os.path.normcase(self.session_data()['state']), os.path.normcase(p))

    def test_forward_slashes_and_git_bash(self):
        p = self.state()
        self.tool('Edit', p.replace('\\', '/'))
        self.assertEqual(os.path.normcase(self.session_data()['state']), os.path.normcase(p))
        if os.name != 'nt':
            self.skipTest('the /c/... form only exists with Git Bash on Windows')
        bash_style = '/' + p[0].lower() + p[2:].replace('\\', '/')
        self.tool('Write', bash_style, sid='s2')
        self.assertEqual(os.path.normcase(self.session_data('s2')['state']), os.path.normcase(p))
        self.assertTrue(os.path.exists(self.session_data('s2')['state']))

    def test_other_files_do_not_link(self):
        self.tool('Write', os.path.join(self.tmp, 'other.md'))
        self.tool('Write', os.path.join(self.state_dir, '_TEMPLATE.md'))
        self.tool('Read', self.state())
        self.assertNotIn('state', self.session_data())

    def test_the_first_state_file_wins(self):
        p1, p2 = self.state('first.md'), self.state('second.md')
        self.tool('Write', p1)
        self.tool('Write', p2)
        d = self.session_data()
        self.assertEqual(os.path.normcase(d['state']), os.path.normcase(p1))
        self.assertIn(p2, d['other_states_written'])

    def test_subagent_is_ignored(self):
        self.tool('Write', self.state(), agent_id='a123', agent_type='Explore')
        self.assertEqual(self.session_data(), {})


class Reminders(Base):
    def test_below_threshold_nothing(self):
        self.write_transcript([usage_line(250_000)])
        self.assertIsNone(self.tool())

    def test_above_threshold_without_state_asks_to_create_it(self):
        self.write_transcript([usage_line(310_000)])
        out = self.tool()
        text = out['hookSpecificOutput']['additionalContext']
        self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertIn('310k', text)
        self.assertIn('367k', text)                        # 400k - 33k
        self.assertIn('create it now', text)

    def test_the_reminder_is_a_rule_not_a_suggestion(self):
        self.write_transcript([usage_line(310_000)])
        text = self.tool()['hookSpecificOutput']['additionalContext']
        self.assertIn("It's a working rule, not a suggestion", text)

    def test_above_threshold_with_old_state_asks_to_update_it(self):
        p = self.state(age_s=3600)
        self.tool('Write', p)
        self.write_transcript([usage_line(320_000)])
        text = self.tool()['hookSpecificOutput']['additionalContext']
        self.assertIn(p, text)
        self.assertIn('update', text)

    def test_fresh_state_nothing(self):
        self.tool('Write', self.state(age_s=60))
        self.write_transcript([usage_line(320_000)])
        self.assertIsNone(self.tool())

    def test_pause_between_reminders_and_urgent(self):
        self.tool('Write', self.state(age_s=3600))
        self.write_transcript([usage_line(320_000)])
        self.assertIsNotNone(self.tool())
        self.assertIsNone(self.tool())                     # within 10 minutes: nothing
        self.write_transcript([usage_line(350_000)])       # above 340k: urgent, once
        self.assertIn('URGENT', self.tool()['hookSpecificOutput']['additionalContext'])
        self.assertIsNone(self.tool())

    def test_ignores_subagents_and_synthetic_lines(self):
        self.write_transcript([usage_line(200_000), usage_line(900_000, sidechain=True),
                               usage_line(900_000, model='<synthetic>')])
        self.assertIsNone(self.tool())

    def test_after_compaction_does_not_use_the_old_size(self):
        self.write_transcript([usage_line(380_000), json.dumps({'type': 'system', 'subtype': 'compact_boundary'})])
        self.assertIsNone(self.tool())

    def test_huge_line_at_the_tail(self):
        huge = json.dumps({'type': 'api-request-blob', 'blob': 'x' * (3 << 20)})
        self.write_transcript([usage_line(320_000), huge])
        self.assertIsNotNone(self.tool())

    def test_test_threshold_from_environment(self):
        self.env['MEMORY_THRESHOLD'] = '1000'
        self.write_transcript([usage_line(5_000)])
        self.assertIsNotNone(self.tool())

    def test_missing_transcript(self):
        self.assertIsNone(self.tool())

    def test_first_margin_from_environment(self):
        self.env['MEMORY_FIRST_MARGIN'] = '300000'         # first reminder at 400k - 300k = 100k
        self.write_transcript([usage_line(120_000)])
        self.assertIsNotNone(self.tool())

    def test_urgent_margin_from_environment(self):
        self.env['MEMORY_URGENT_MARGIN'] = '100000'        # urgent from 300k
        self.tool('Write', self.state(age_s=3600))
        self.write_transcript([usage_line(305_000)])
        self.assertIn('URGENT', self.tool()['hookSpecificOutput']['additionalContext'])

    def test_urgent_asks_for_a_fresher_state_than_the_normal_reminder(self):
        self.tool('Write', self.state(age_s=7 * 60))       # older than 5 min, newer than 15
        self.write_transcript([usage_line(320_000)])
        self.assertIsNone(self.tool())                     # normal: still fresh
        self.write_transcript([usage_line(350_000)])
        self.assertIn('URGENT', self.tool()['hookSpecificOutput']['additionalContext'])

    def test_state_found_from_the_transcript_when_never_registered(self):
        p = self.state(age_s=3600)
        self.write_transcript([tool_line(p, 'Edit'), usage_line(320_000)])
        self.assertIn(p, self.tool()['hookSpecificOutput']['additionalContext'])

    def test_a_state_file_that_was_only_read_is_not_taken(self):
        other = self.state('worker-1.md', age_s=60)          # another session's state, read by a reviewer
        self.write_transcript([tool_line(other, 'Read'),
                               json.dumps({'type': 'user', 'message': {'content': f'see {other}'}}),
                               usage_line(320_000)])
        text = self.tool()['hookSpecificOutput']['additionalContext']
        self.assertNotIn(other, text)
        self.assertNotIn('state', self.session_data())

    def test_bad_window_falls_back_to_one_million(self):
        self.env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] = 'not a number'
        self.write_transcript([usage_line(950_000)])
        text = self.tool()['hookSpecificOutput']['additionalContext']
        self.assertIn('967k', text)                        # 1,000k - 33k


class Compaction(Base):
    def compact(self, trigger='auto', summary='', sid='s1'):
        self.run_hook('pre-compact', {'session_id': sid, 'transcript_path': self.transcript, 'trigger': trigger})
        self.run_hook('post-summary', {'session_id': sid, 'transcript_path': self.transcript, 'trigger': trigger,
                                       'compact_summary': summary})
        return self.run_hook('after-compact', {'session_id': sid, 'transcript_path': self.transcript,
                                               'hook_event_name': 'SessionStart', 'source': 'compact'})

    def test_puts_the_state_file_back(self):
        p = self.state(text='# Next step\nStep 2: rename "draft" -> "final", then run the tests.\n', age_s=600)
        self.tool('Write', p)
        self.write_transcript([usage_line(370_000)])
        out = self.compact(summary=f'... state file {p} ...')
        text = out['hookSpecificOutput']['additionalContext']
        self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'SessionStart')
        self.assertIn('(automatic)', text)
        self.assertIn('Step 2: rename "draft" -> "final", then run the tests.', text)
        self.assertIn('--- STATE FILE ---', text)
        self.assertIn('--- END OF STATE FILE ---', text)
        c = self.session_data()['compactions'][-1]
        self.assertEqual(c['trigger'], 'auto')
        self.assertEqual(c['context'], 370_000)
        self.assertTrue(c['summary_mentions_state'])

    def test_long_file_is_truncated_under_9500(self):
        p = self.state(text='state line\n' * 2000)
        self.tool('Write', p)
        text = self.compact(trigger='manual')['hookSpecificOutput']['additionalContext']
        self.assertLessEqual(len(text), 9_500)
        self.assertIn('truncated', text)
        self.assertIn('(manual)', text)

    def test_finds_the_state_from_the_transcript(self):
        p = self.state('reviewer.md')
        self.write_transcript([tool_line(p)])
        text = self.compact()['hookSpecificOutput']['additionalContext']
        self.assertIn(p, text)
        self.assertEqual(os.path.normcase(self.session_data()['state']), os.path.normcase(p))
        self.assertFalse(self.session_data()['compactions'][-1]['summary_mentions_state'])

    def test_finds_the_state_with_a_folder_of_another_name(self):
        other = os.path.join(self.tmp, 'status')
        os.makedirs(other)
        self.env['MEMORY_STATE_DIR'] = other
        p = os.path.join(other, 'auto-check.md')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('VERIFICATION-CODE: 4826-BRAVO\n')
        self.write_transcript([tool_line(p)])
        self.assertIn('4826-BRAVO', self.compact()['hookSpecificOutput']['additionalContext'])

    def test_a_tilde_in_the_state_folder_is_expanded(self):
        home = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(home, 'notes'))
        self.env['HOME'] = self.env['USERPROFILE'] = home
        self.env['MEMORY_STATE_DIR'] = '~/notes'
        p = os.path.join(home, 'notes', 'tilde.md')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('VERIFICATION-CODE: 7731-TILDE\n')
        self.tool('Write', p)
        self.assertIn('7731-TILDE', self.compact()['hookSpecificOutput']['additionalContext'])

    def test_without_a_state_asks_to_create_it(self):
        text = self.compact()['hookSpecificOutput']['additionalContext']
        self.assertIn('has no state file', text)

    def test_old_state_warns(self):
        self.tool('Write', self.state(age_s=3 * 3600))
        self.assertIn('Warning', self.compact()['hookSpecificOutput']['additionalContext'])

    def test_other_starts_and_subagents_are_ignored(self):
        self.tool('Write', self.state())
        base = {'session_id': 's1', 'transcript_path': self.transcript, 'hook_event_name': 'SessionStart'}
        self.assertIsNone(self.run_hook('after-compact', dict(base, source='startup')))
        self.assertIsNone(self.run_hook('after-compact', dict(base, source='compact', agent_id='a1')))

    def test_cp1252_file(self):
        p = os.path.join(self.state_dir, 'old.md')
        with open(p, 'w', encoding='cp1252') as fh:
            fh.write('# Next step\ncafé crème\n')
        self.tool('Write', p)
        self.assertIn('café crème', self.compact()['hookSpecificOutput']['additionalContext'])

    def test_pre_compact_and_post_summary_never_print(self):
        self.tool('Write', self.state())
        self.assertIsNone(self.run_hook('pre-compact', {'session_id': 's1', 'trigger': 'auto'}))
        self.assertIsNone(self.run_hook('post-summary', {'session_id': 's1', 'trigger': 'auto', 'compact_summary': 'x'}))

    def test_post_summary_without_state_records_the_summary_size(self):
        self.run_hook('post-summary', {'session_id': 's1', 'trigger': 'manual', 'compact_summary': 'abcdef'})
        c = self.session_data()['compactions'][-1]
        self.assertEqual(c['summary_chars'], 6)
        self.assertFalse(c['summary_mentions_state'])

    def test_compaction_hooks_skip_subagents(self):
        self.run_hook('pre-compact', {'session_id': 's1', 'trigger': 'auto', 'agent_id': 'a1'})
        self.run_hook('post-summary', {'session_id': 's1', 'trigger': 'auto', 'agent_id': 'a1', 'compact_summary': 'x'})
        self.assertEqual(self.session_data(), {})


class Robustness(Base):
    def test_broken_or_empty_input(self):
        for command in ('after-tool', 'pre-compact', 'post-summary', 'after-compact', 'unknown'):
            for raw in ('', '{not json', '[]'):
                r = subprocess.run([sys.executable, SCRIPT, command], input=raw.encode(), capture_output=True,
                                   env=self.env, timeout=30)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(r.stdout, b'')

    def test_timing(self):
        self.write_transcript([usage_line(200_000)] * 2000)
        t = time.time()
        self.tool()
        self.assertLess(time.time() - t, 2.0)

    def test_an_internal_error_exits_zero_and_logs_one_entry(self):
        payload = json.dumps({'session_id': 's1', 'tool_name': 'Write', 'tool_input': 'not a dict'})
        r = subprocess.run([sys.executable, SCRIPT, 'after-tool'], input=payload.encode(), capture_output=True,
                           env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b'')
        with open(os.path.join(self.data, 'errors.log'), encoding='utf-8') as fh:
            log = fh.read()
        self.assertEqual(log.count('--- '), 1)
        self.assertIn('after-tool', log)

    def test_non_numeric_margins_do_not_break_the_hook(self):
        self.env['MEMORY_FIRST_MARGIN'] = 'a lot'
        self.env['MEMORY_URGENT_MARGIN'] = ''
        self.write_transcript([usage_line(250_000)])
        self.assertIsNone(self.tool())                     # falls back to the defaults: 300k is the first reminder
        self.write_transcript([usage_line(310_000)])
        self.assertIsNotNone(self.tool())

    def test_default_folders_live_under_the_home_directory(self):
        home = os.path.join(self.tmp, 'home')
        os.makedirs(home)
        env = dict(self.env, HOME=home, USERPROFILE=home)
        env.pop('MEMORY_STATE_DIR')
        env.pop('MEMORY_DATA_DIR')
        state_dir = os.path.join(home, '.claude', 'session-state')
        os.makedirs(state_dir)
        p = os.path.join(state_dir, 'mine.md')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('x')
        payload = {'session_id': 's9', 'tool_name': 'Write', 'tool_input': {'file_path': p}}
        r = subprocess.run([sys.executable, SCRIPT, 'after-tool'], input=json.dumps(payload).encode(),
                           capture_output=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(home, '.claude', 'memory-sessions', 's9.json')))


class GitBashPaths(unittest.TestCase):
    """The /c/... form (Git Bash) is a drive path on Windows only; elsewhere the path is left alone.

    The tests pass the platform in, so they check both cases on whatever system they run on.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'hooks'))
        import memory_hook
        cls.hook = memory_hook

    @staticmethod
    def slashes(p):
        return p.replace('\\', '/')

    def test_on_windows_a_git_bash_path_becomes_a_drive_path(self):
        self.assertEqual(self.slashes(self.hook.real_path('/c/Users/me/notes/a.md', windows=True)), 'C:/Users/me/notes/a.md')
        self.assertEqual(self.slashes(self.hook.real_path('/D/x/y.md', windows=True)), 'D:/x/y.md')

    def test_off_windows_the_path_is_left_as_it_is(self):
        self.assertEqual(self.slashes(self.hook.real_path('/c/Users/me/notes/a.md', windows=False)), '/c/Users/me/notes/a.md')

    def test_other_paths_are_normalised_in_both_cases(self):
        for windows in (True, False):
            self.assertEqual(self.slashes(self.hook.real_path('/tmp/a/../b.md', windows=windows)), '/tmp/b.md')
            self.assertEqual(self.hook.real_path('', windows=windows), '')

    def test_default_follows_the_running_system(self):
        expected = self.hook.real_path('/c/x/y.md', windows=(os.name == 'nt'))
        self.assertEqual(self.hook.real_path('/c/x/y.md'), expected)


class ExampleSettings(unittest.TestCase):
    """hooks/settings.example.json must wire the four commands the script really has."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(HERE), 'hooks', 'settings.example.json'), encoding='utf-8') as fh:
            self.settings = json.load(fh)

    def commands(self):
        found = {}
        for event, entries in self.settings['hooks'].items():
            for entry in entries:
                for h in entry['hooks']:
                    found[event] = h['command'].rsplit(' ', 1)[-1]
        return found

    def test_events_map_to_the_four_commands(self):
        self.assertEqual(self.commands(), {'PostToolUse': 'after-tool', 'PreCompact': 'pre-compact',
                                           'PostCompact': 'post-summary', 'SessionStart': 'after-compact'})

    def test_only_the_compact_session_start_is_hooked(self):
        self.assertEqual(self.settings['hooks']['SessionStart'][0]['matcher'], 'compact')

    def test_recommended_environment(self):
        self.assertEqual(sorted(self.settings['env']), ['CLAUDE_CODE_AUTO_COMPACT_WINDOW',
                                                        'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP',
                                                        'CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL'])


if __name__ == '__main__':
    unittest.main()
