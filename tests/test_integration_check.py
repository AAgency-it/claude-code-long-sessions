"""Tests for the pure parts of scripts/integration_check.py (the part that calls `claude` is not tested here)."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import integration_check as ic  # noqa: E402

HOOK = '/some dir/claude-code-long-sessions/hooks/memory_hook.py'


class BuildSettings(unittest.TestCase):
    def setUp(self):
        self.s = ic.build_settings(HOOK, '/tmp/state', '/tmp/data', python='python')

    def test_four_hooks_with_the_right_events_and_commands(self):
        h = self.s['hooks']
        self.assertEqual(sorted(h), ['PostCompact', 'PostToolUse', 'PreCompact', 'SessionStart'])
        for event, command in (('PostToolUse', 'after-tool'), ('PreCompact', 'pre-compact'),
                               ('PostCompact', 'post-summary'), ('SessionStart', 'after-compact')):
            cmd = h[event][0]['hooks'][0]['command']
            self.assertTrue(cmd.endswith(' ' + command), cmd)
            self.assertEqual(h[event][0]['hooks'][0]['type'], 'command')

    def test_only_the_compact_start_is_hooked(self):
        self.assertEqual(self.s['hooks']['SessionStart'][0]['matcher'], 'compact')

    def test_paths_with_spaces_are_quoted(self):
        cmd = self.s['hooks']['PostToolUse'][0]['hooks'][0]['command']
        self.assertIn('"/some dir/claude-code-long-sessions/hooks/memory_hook.py"', cmd)

    def test_environment_points_the_hook_at_the_temporary_folders(self):
        self.assertEqual(self.s['env']['MEMORY_STATE_DIR'], '/tmp/state')
        self.assertEqual(self.s['env']['MEMORY_DATA_DIR'], '/tmp/data')

    def test_window_is_optional(self):
        self.assertNotIn('CLAUDE_CODE_AUTO_COMPACT_WINDOW', self.s['env'])
        s = ic.build_settings(HOOK, '/tmp/state', '/tmp/data', window=100000, python='python')
        self.assertEqual(s['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '100000')

    def test_settings_are_json_serialisable(self):
        json.dumps(self.s)

    def test_backslashes_become_forward_slashes_in_commands(self):
        s = ic.build_settings('C:\\kit\\hooks\\memory_hook.py', 'C:\\s', 'C:\\d', python='py')
        self.assertIn('"C:/kit/hooks/memory_hook.py"', s['hooks']['PreCompact'][0]['hooks'][0]['command'])


class ParsingClaudeOutput(unittest.TestCase):
    def test_reads_the_session_id_and_the_answer(self):
        out = json.dumps({'type': 'result', 'session_id': 'abc-123', 'result': 'DONE'})
        self.assertEqual(ic.parse_result(out), ('abc-123', 'DONE'))

    def test_survives_text_around_the_json(self):
        out = 'warning: something\n' + json.dumps({'session_id': 's', 'result': 'x'}) + '\n'
        self.assertEqual(ic.parse_result(out), ('s', 'x'))

    def test_unparseable_output_gives_none(self):
        self.assertEqual(ic.parse_result('not json at all'), (None, ''))


class Instructions(unittest.TestCase):
    def test_compact_instructions_template_is_installed_in_the_project(self):
        text = ic.project_claude_md(ROOT)
        self.assertIn('Compact instructions', text)

    def test_random_code_is_unguessable_and_distinct(self):
        self.assertNotEqual(ic.new_code(), ic.new_code())
        self.assertRegex(ic.new_code(), r'^[A-Z0-9-]{8,}$')


if __name__ == '__main__':
    unittest.main()
