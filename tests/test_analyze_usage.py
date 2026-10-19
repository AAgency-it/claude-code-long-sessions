"""Tests for tools/analyze_usage.py, on synthetic transcripts (tests/fixtures/projects).

Usage, from the repository root: py -3 -m unittest discover -s tests -v
"""
import collections
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, 'tools', 'analyze_usage.py')
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import analyze_usage as au  # noqa: E402

PROJECTS = os.path.join(HERE, 'fixtures', 'projects')
MAIN_1 = os.path.join(PROJECTS, 'proj-a', 'sess-main-1.jsonl')
SUB_1 = os.path.join(PROJECTS, 'proj-a', 'sess-main-1', 'subagents', 'agent-a1.jsonl')


def when(s):
    d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fake_call(second, ctx, out=0, seg=0, fam='sonnet', trigger='person: message', tools=1):
    """A call record as analyze_transcript builds it, with just what the simulation reads."""
    ts = datetime.fromtimestamp(1_700_000_000 + second, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
    return dict(ts=ts, ctx=ctx, out=out, seg=seg, fam=fam, trigger=trigger, tools=tools)


class Classification(unittest.TestCase):
    def test_person_message_and_command(self):
        self.assertEqual(au.classify_user('please do X', False), 'person: message')
        self.assertEqual(au.classify_user('<command-name>/compact</command-name>', False), 'person: command')

    def test_goal_rerun_is_recognised_even_when_flagged_meta(self):
        self.assertEqual(au.classify_user('Stop hook feedback: keep going', True), 'goal: re-run after stop')

    def test_goal_check_in(self):
        self.assertEqual(au.classify_user('Goal check-in: are we there yet?', False), 'goal: check-in')

    def test_message_from_another_session_even_when_meta(self):
        text = 'Another Claude session sent a message\n<cross-session-message from-name="w">hi</cross-session-message>'
        self.assertEqual(au.classify_user(text, True), 'message from another session')
        self.assertEqual(au.classify_user('[Cross-session] hello', True), 'message from another session')

    def test_notifications(self):
        self.assertEqual(au.classify_user('<task-notification>Monitor event: line</task-notification>', False),
                         'notification: monitor')
        agent = '<task-notification><status>done</status><output-file>x</output-file>agent finished</task-notification>'
        self.assertEqual(au.classify_user(agent, False), 'notification: subagent finished')
        self.assertEqual(au.classify_user('<task-notification>build done</task-notification>', False),
                         'notification: background work')

    def test_resume_after_compaction(self):
        self.assertEqual(au.classify_user('This session is being continued from a previous conversation', False),
                         'resume after compaction')

    def test_reminders_and_skills_are_not_triggers(self):
        for text in ('<system-reminder>x</system-reminder>', 'Base directory for this skill: /x', '[Image #1]',
                     'Caveat: the messages below', '[Usage limit reached]'):
            self.assertIsNone(au.classify_user(text, False), text)

    def test_other_meta_messages_are_not_triggers(self):
        self.assertIsNone(au.classify_user('something injected by the harness', True))

    def test_trigger_groups_and_shares(self):
        events = collections.Counter({
            'message from another session': 48, 'goal: re-run after stop': 30, 'goal: check-in': 2,
            'notification: monitor': 5, 'notification: subagent finished': 8, 'person: message': 7,
            'person: command': 1})
        shares = au.trigger_shares(events)
        self.assertEqual({k: round(v) for k, v in shares.items()},
                         {'other session': 48, 'goal': 32, 'notification': 13, 'person': 8})


class ParsingTranscripts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = au.analyze_transcript(MAIN_1)
        cls.calls = cls.r['calls']

    def test_calls_are_deduplicated_by_message_id(self):
        self.assertEqual(len(self.calls), 7)

    def test_a_split_message_keeps_its_final_usage_and_all_its_tools(self):
        m1 = self.calls[0]
        self.assertEqual(m1['out'], 50)
        self.assertEqual(m1['tools'], 1)
        self.assertEqual(m1['w1'], 1000)

    def test_cost_uses_the_price_table(self):
        p = au.PRICES['opus']
        expected = (10 * p['inp'] + 1000 * p['w1'] + 50 * p['out']) / 1e6
        self.assertAlmostEqual(self.calls[0]['total'], expected, places=9)

    def test_each_call_gets_the_trigger_that_started_its_turn(self):
        self.assertEqual([c['trigger'] for c in self.calls], [
            'person: message', 'person: message', 'goal: re-run after stop', 'message from another session',
            'notification: subagent finished', 'resume after compaction', 'person: message'])

    def test_meta_reminders_do_not_change_the_trigger(self):
        self.assertEqual(self.calls[-1]['trigger'], 'person: message')
        self.assertEqual(self.r['trigger_counts']['person: message'], 2)

    def test_compaction_starts_a_new_segment(self):
        self.assertEqual([c['seg'] for c in self.calls], [0, 0, 0, 0, 0, 1, 1])
        self.assertEqual(len(self.r['compactions']), 1)
        self.assertEqual(self.r['compactions'][0]['trigger'], 'auto')
        self.assertEqual(self.r['compactions'][0]['pre'], 1600)

    def test_messages_from_other_sessions_are_counted_by_sender(self):
        self.assertEqual(len(self.r['cross_messages']), 1)
        self.assertEqual(self.r['cross_messages'][0]['from'], 'worker-1')
        self.assertEqual(self.r['cross_messages'][0]['chars'], len('Phase 1 is done'))

    def test_context_is_input_plus_cache(self):
        self.assertEqual(self.calls[1]['ctx'], 5 + 200 + 1000)

    def test_unknown_models_are_priced_as_the_default_family(self):
        self.assertEqual(au.family('some-future-model'), au.DEFAULT_FAMILY)
        self.assertEqual(au.family('claude-sonnet-5-5'), 'sonnet')
        self.assertEqual(au.family('claude-haiku-5'), 'haiku')

    def test_since_and_until_cut_the_transcript(self):
        late = au.analyze_transcript(MAIN_1, since=when('2026-01-10T10:01:30Z'))
        self.assertEqual(len(late['calls']), 2)
        self.assertEqual(len(late['compactions']), 1)
        early = au.analyze_transcript(MAIN_1, until=when('2026-01-10T10:00:12Z'))
        self.assertEqual(len(early['calls']), 1)
        self.assertEqual(len(early['compactions']), 0)

    def test_broken_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 's.jsonl')
            with open(MAIN_1, encoding='utf-8') as src, open(p, 'w', encoding='utf-8') as out:
                out.write('{not json\n\n' + src.read())
            self.assertEqual(len(au.analyze_transcript(p)['calls']), 7)

    def test_summary(self):
        s = au.summarize(self.r)
        self.assertEqual(s['calls'], 7)
        self.assertEqual(s['models'], {'claude-opus-5-5': 7})
        self.assertAlmostEqual(s['cache_read_share'], s['cost']['rd'] / s['cost']['total'], places=9)
        self.assertEqual(s['context_max'], max(c['ctx'] for c in self.calls))
        self.assertEqual(s['by_trigger']['person: message']['calls'], 3)

    def test_summary_of_nothing_is_none(self):
        self.assertIsNone(au.summarize(au.analyze_transcript(MAIN_1, since=when('2030-01-01'))))


class Attribution(unittest.TestCase):
    def write(self, d, entries):
        p = os.path.join(d, 's.jsonl')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(json.dumps(e) for e in entries) + '\n')
        return p

    def call(self, mid, second, w1, out, inp=5):
        return {'type': 'assistant', 'timestamp': f'2026-01-10T10:00:{second:02d}Z', 'message': {
            'id': mid, 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'x'}],
            'usage': {'input_tokens': inp, 'output_tokens': out, 'cache_creation_input_tokens': w1,
                      'cache_read_input_tokens': 0, 'cache_creation': {'ephemeral_5m_input_tokens': 0,
                                                                          'ephemeral_1h_input_tokens': w1}}}}

    def test_a_huge_jump_in_new_tokens_is_a_cache_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, [
                self.call('a', 1, 5000, 100),
                {'type': 'user', 'timestamp': '2026-01-10T10:00:10Z', 'message': {'content': 'short note'}},
                self.call('b', 20, 60000, 50),
            ])
            attr = au.analyze_transcript(p)['attribution']
        rewrite = attr[au.LABEL_REWRITE]
        self.assertAlmostEqual(rewrite['tok'], 60005 - 100 - len('short note') / au.CHARS_PER_TOKEN, delta=1)
        self.assertEqual(attr[au.LABEL_RESPONSE]['tok'], 100)
        self.assertIn('text: person: message', attr)

    def test_ordinary_growth_is_attributed_to_what_entered_the_context(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.write(d, [
                self.call('a', 1, 5000, 100),
                {'type': 'user', 'timestamp': '2026-01-10T10:00:10Z', 'message': {'content': 'x' * 3300}},
                self.call('b', 20, 1200, 50),
            ])
            attr = au.analyze_transcript(p)['attribution']
        self.assertNotIn(au.LABEL_REWRITE, attr)
        self.assertAlmostEqual(attr['text: person: message']['tok'], 1105, delta=1)
        self.assertEqual(attr[au.LABEL_BASE]['tok'], 5005)

    def test_tool_results_are_attributed_to_the_tool_by_name(self):
        r = au.analyze_transcript(MAIN_1)
        self.assertIn('tool: Bash', r['attribution'])
        self.assertIn('tool: Read', r['attribution'])


class Discovery(unittest.TestCase):
    def test_finds_sessions_and_their_subagents(self):
        found = {s['id']: s for s in au.discover_sessions(PROJECTS)}
        self.assertEqual(sorted(found), ['sess-main-1', 'sess-main-2'])
        subs = found['sess-main-1']['subagents']
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]['meta']['agentType'], 'Explore')
        self.assertEqual(found['sess-main-2']['subagents'], [])

    def test_session_filter_is_repeatable(self):
        self.assertEqual([s['id'] for s in au.discover_sessions(PROJECTS, ['sess-main-2'])], ['sess-main-2'])
        self.assertEqual(sorted(s['id'] for s in au.discover_sessions(PROJECTS, ['sess-main-1', 'sess-main-2'])),
                         ['sess-main-1', 'sess-main-2'])

    def test_accepts_a_single_project_folder(self):
        ids = sorted(s['id'] for s in au.discover_sessions(os.path.join(PROJECTS, 'proj-a')))
        self.assertEqual(ids, ['sess-main-1', 'sess-main-2'])

    def test_missing_folder_gives_nothing(self):
        self.assertEqual(au.discover_sessions(os.path.join(PROJECTS, 'nope')), [])

    def test_subagent_calls_are_kept_apart_from_the_main_session(self):
        sub = au.analyze_transcript(SUB_1)
        self.assertEqual([c['fam'] for c in sub['calls']], ['sonnet', 'sonnet'])
        self.assertEqual(len(au.analyze_transcript(MAIN_1)['calls']), 7)


class Scenarios(unittest.TestCase):
    def test_steady_cache_costs_a_read_plus_the_new_tokens(self):
        p = au.PRICES['sonnet']
        calls = [fake_call(0, 10_000), fake_call(10, 10_500)]
        expected = (10_000 * p['w1'] + 10_000 * p['rd'] + 500 * p['w1']) / 1e6
        self.assertAlmostEqual(au.simulate(calls, 10**7, '1h'), expected, places=9)

    def test_an_expired_cache_is_rewritten_in_full(self):
        p = au.PRICES['sonnet']
        calls = [fake_call(0, 10_000), fake_call(600, 10_500)]       # 10 minutes of pause
        five = au.simulate(calls, 10**7, '5m')
        one = au.simulate(calls, 10**7, '1h')
        self.assertAlmostEqual(five, (10_000 * p['w5'] + 10_500 * p['w5']) / 1e6, places=9)
        self.assertGreater(five, one)

    def test_compaction_pays_off_in_a_long_session(self):
        calls = [fake_call(10 * i, 100_000 + 1000 * i) for i in range(300)]
        base = au.simulate(calls, 10**7, '1h')
        self.assertLess(au.simulate(calls, 200_000, '1h'), base)
        self.assertLess(au.simulate(calls, 150_000, '1h'), au.simulate(calls, 250_000, '1h'))

    def test_a_new_segment_restarts_from_its_own_size(self):
        calls = [fake_call(0, 300_000), fake_call(10, 20_000, seg=1), fake_call(20, 20_500, seg=1)]
        p = au.PRICES['sonnet']
        expected = (300_000 * p['w1'] + 20_000 * p['w1'] + 20_000 * p['rd'] + 500 * p['w1']) / 1e6
        self.assertAlmostEqual(au.simulate(calls, 10**7, '1h'), expected, places=9)

    def test_cap_on_idle_goal_reruns(self):
        rerun = 'goal: re-run after stop'
        calls = ([fake_call(i, 1, trigger=rerun, tools=0) for i in range(5)] + [fake_call(10, 1)]
                 + [fake_call(20 + i, 1, trigger=rerun, tools=0) for i in range(3)])
        self.assertEqual(len(au.cap_reruns(calls, 2)), 5)
        self.assertEqual(len(au.cap_reruns(calls, None)), 9)
        self.assertEqual(len(au.cap_reruns(calls, 0)), 1)

    def test_a_rerun_that_uses_tools_is_never_cut_and_resets_the_count(self):
        rerun = 'goal: re-run after stop'
        calls = [fake_call(0, 1, trigger=rerun, tools=0), fake_call(1, 1, trigger=rerun, tools=0),
                 fake_call(2, 1, trigger=rerun, tools=2),
                 fake_call(3, 1, trigger=rerun, tools=0), fake_call(4, 1, trigger=rerun, tools=0),
                 fake_call(5, 1, trigger=rerun, tools=0)]
        self.assertEqual(len(au.cap_reruns(calls, 2)), 5)

    def test_peak_of_idle_reruns_in_one_hour(self):
        rerun = 'goal: re-run after stop'
        calls = [fake_call(12 * i, 1, trigger=rerun, tools=0) for i in range(298)] + [fake_call(9000, 1, trigger=rerun, tools=0)]
        peak, every = au.idle_rerun_peak(calls)
        self.assertEqual(peak, 298)
        self.assertAlmostEqual(every, 3600 / 298, delta=0.01)       # one every 12 seconds, not the median gap
        self.assertEqual(au.idle_rerun_peak([fake_call(0, 1)]), (0, None))

    def test_the_interval_is_the_hour_divided_by_the_count_even_when_the_re_runs_come_in_bursts(self):
        rerun = 'goal: re-run after stop'
        seconds = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 1800, 3000]       # a burst of ten, then two stragglers
        calls = [fake_call(s, 1, trigger=rerun, tools=0) for s in seconds]
        peak, every = au.idle_rerun_peak(calls)
        self.assertEqual(peak, 12)
        self.assertAlmostEqual(every, 300, delta=0.01)             # 3600 / 12, whatever the burst median is

class IdleIsDecidedPerTurn(unittest.TestCase):
    """A /goal re-run that used a tool did work, even if its last call answers with text alone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='analyze usage test ')

    def transcript(self, name, lines):
        p = os.path.join(self.tmp, name)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(json.dumps(x) for x in lines) + '\n')
        return p

    @staticmethod
    def call(mid, second, tools=0):
        content = [{'type': 'tool_use', 'id': f'tu-{mid}', 'name': 'Bash', 'input': {}}] * tools or [{'type': 'text', 'text': 'ok'}]
        return {'type': 'assistant', 'timestamp': f'2026-01-10T10:00:{second:02d}Z', 'message': {
            'id': mid, 'model': 'claude-sonnet-5-5', 'content': content,
            'usage': {'input_tokens': 2, 'cache_creation_input_tokens': 100, 'cache_read_input_tokens': 10_000,
                      'output_tokens': 50}}}

    @staticmethod
    def rerun(second):
        return {'type': 'user', 'timestamp': f'2026-01-10T10:00:{second:02d}Z',
                'message': {'content': 'Stop hook feedback: the goal is not reached yet'}}

    def test_a_productive_rerun_is_not_idle(self):
        p = self.transcript('busy.jsonl', [
            self.rerun(0), self.call('m1', 1, tools=1),
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tu-m1', 'content': 'x'}]}},
            self.call('m2', 2),                                   # the closing text of the same turn
            self.rerun(3), self.call('m3', 4)])                   # a truly idle re-run
        calls = au.analyze_transcript(p)['calls']
        self.assertEqual([au.idle_rerun(c) for c in calls], [False, False, True])
        self.assertEqual(len(au.cap_reruns(calls, 0)), 2)         # only the idle turn is cut
        self.assertEqual(au.idle_rerun_peak(calls)[0], 1)

    def test_a_call_counted_in_an_earlier_transcript_is_not_counted_again(self):
        first = self.transcript('a.jsonl', [self.rerun(0), self.call('m1', 1), self.call('m2', 2)])
        second = self.transcript('b.jsonl', [self.rerun(0), self.call('m1', 1), self.call('m2', 2),
                                             self.call('m9', 5)])
        seen = set()
        self.assertEqual(len(au.analyze_transcript(first, seen=seen)['calls']), 2)
        self.assertEqual([c['total'] > 0 for c in au.analyze_transcript(second, seen=seen)['calls']], [True])


class WhatStartedTheTurns(unittest.TestCase):
    """The share is weighed on COST: every call of a turn counts for the trigger that started the turn."""

    def priced(self, trigger, total):
        c = fake_call(0, 1, trigger=trigger)
        c['total'] = total
        return c

    def test_cost_is_assigned_to_the_group_of_the_trigger(self):
        calls = [self.priced('message from another session', 3.0), self.priced('message from another session', 3.0),
                 self.priced('goal: re-run after stop', 2.0), self.priced('goal: check-in', 0.5),
                 self.priced('notification: monitor', 1.0), self.priced('person: message', 0.5)]
        shares = au.trigger_cost_shares([calls])
        self.assertAlmostEqual(shares['other session'], 60.0, places=6)
        self.assertAlmostEqual(shares['goal'], 25.0, places=6)
        self.assertAlmostEqual(shares['notification'], 10.0, places=6)
        self.assertAlmostEqual(shares['person'], 5.0, places=6)
        self.assertAlmostEqual(sum(shares.values()), 100.0, places=6)

    def test_a_turn_with_many_calls_weighs_more_than_one_with_a_single_call(self):
        # one message from a person started ten expensive calls; ten notifications started one cheap call each
        calls = [self.priced('person: message', 1.0)] * 10 + [self.priced('notification: monitor', 0.1)] * 10
        events = au.trigger_shares(collections.Counter({'person: message': 1, 'notification: monitor': 10}))
        costs = au.trigger_cost_shares([calls])
        self.assertLess(events['person'], 10)
        self.assertGreater(costs['person'], 90)

    def test_several_sessions_are_pooled(self):
        a = [self.priced('person: message', 1.0)]
        b = [self.priced('goal: re-run after stop', 3.0)]
        shares = au.trigger_cost_shares([a, b])
        self.assertAlmostEqual(shares['person'], 25.0, places=6)
        self.assertAlmostEqual(shares['goal'], 75.0, places=6)

    def test_nothing_gives_nothing(self):
        self.assertEqual(au.trigger_cost_shares([]), {})
        self.assertEqual(au.trigger_cost_shares([[self.priced('person: message', 0.0)]]), {})

    def test_on_the_fixture(self):
        calls = au.analyze_transcript(MAIN_1)['calls']
        total = sum(c['total'] for c in calls)
        person = sum(c['total'] for c in calls if c['trigger'].startswith('person:'))
        goal = calls[2]['total']                       # the only call started by the goal re-run
        shares = au.trigger_cost_shares([calls])
        self.assertAlmostEqual(shares['person'], 100 * person / total, places=6)
        self.assertAlmostEqual(shares['goal'], 100 * goal / total, places=6)
        self.assertIn('other session', shares)
        self.assertIn('notification', shares)
        self.assertIn('other', shares)                 # the call that resumed after the compaction
        self.assertAlmostEqual(sum(shares.values()), 100.0, places=6)


class Command(unittest.TestCase):
    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = au.main(['--projects-dir', PROJECTS, *args])
        return code, buf.getvalue()

    def run_json(self, *args):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'report.json')
            code, text = self.run_main('--out', out, *args)
            with open(out, encoding='utf-8') as fh:
                return code, text, json.load(fh)

    def test_report_and_table(self):
        code, text, rep = self.run_json()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(rep['sessions']), ['sess-main-1', 'sess-main-2'])
        self.assertEqual(rep['totals']['main_calls'], 8)
        self.assertEqual(rep['totals']['subagent_calls'], 2)
        self.assertIn('sess-main-1', text)
        self.assertIn('cache reads', text.lower())

    def test_turn_starters_are_weighed_on_cost_with_events_as_the_second_line(self):
        _, text, rep = self.run_json()
        t = rep['totals']
        self.assertAlmostEqual(sum(t['trigger_cost_shares'].values()), 100.0, places=6)
        self.assertIn('person', t['trigger_cost_shares'])
        self.assertIn('person', t['trigger_shares'])                  # the share of events stays in the JSON
        lines = text.splitlines()
        main_line = next(i for i, l in enumerate(lines) if 'share of main-session cost' in l)
        events_line = next(i for i, l in enumerate(lines) if 'share of events' in l)
        self.assertLess(main_line, events_line)
        self.assertIn('other session', text)

    def test_default_cap_on_idle_reruns_is_3(self):
        _, _, rep = self.run_json()
        names = [s['name'] for s in rep['scenarios']]
        self.assertIn('Cap of 3 idle goal re-runs', names)
        self.assertIn('Compaction at 400k + cap of 3 + subagent cache TTL 1h', names)
        args = au.build_parser().parse_args([])
        self.assertEqual(args.rerun_cap, 3)
        self.assertIn('default 3', ' '.join(au.build_parser().format_help().split()))
        self.assertEqual(au.DEFAULT_RERUN_CAP, 3)

    def test_session_option_selects_sessions(self):
        _, _, rep = self.run_json('--session', 'sess-main-2')
        self.assertEqual(list(rep['sessions']), ['sess-main-2'])
        self.assertEqual(rep['totals']['main_calls'], 1)

    def test_since_and_until(self):
        _, _, rep = self.run_json('--since', '2026-02-01')
        self.assertEqual(list(rep['sessions']), ['sess-main-2'])
        _, _, rep = self.run_json('--until', '2026-01-31')
        self.assertEqual(list(rep['sessions']), ['sess-main-1'])
        _, _, rep = self.run_json('--until', '2026-01-10')       # a plain date includes the whole day
        self.assertEqual(rep['totals']['main_calls'], 7)

    def test_scenarios(self):
        _, text, rep = self.run_json('--thresholds', '500000,400000', '--rerun-cap', '2')
        names = [s['name'] for s in rep['scenarios']]
        self.assertEqual(names[0], 'Baseline (model of the transcripts as they are)')
        self.assertIn('Compaction at 500k', names)
        self.assertIn('Compaction at 400k', names)
        self.assertIn('Cap of 2 idle goal re-runs', names)
        self.assertIn('Subagent cache TTL 1h', names)
        base = rep['scenarios'][0]
        self.assertEqual(base['saved_vs_baseline_pct'], 0)
        self.assertEqual(base['saved_main_pct'], 0)
        sub = [s for s in rep['scenarios'] if s['name'] == 'Subagent cache TTL 1h'][0]
        self.assertLess(sub['subagents'], base['subagents'])
        self.assertEqual(sub['saved_main_pct'], 0)                # only the subagents change
        self.assertGreater(sub['saved_subagents_pct'], 0)
        self.assertIn('Subagent cache TTL 1h', text)
        self.assertIn('main %', text)

    def test_repricing_the_main_sessions(self):
        _, _, rep = self.run_json()
        self.assertGreater(rep['main_as_sonnet']['saved_pct'], 0)

    def test_no_sessions_found(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = au.main(['--projects-dir', os.path.join(PROJECTS, 'nope')])
        self.assertEqual(code, 1)
        self.assertIn('No sessions', buf.getvalue())

    def test_bad_date_is_a_clear_error(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            au.main(['--projects-dir', PROJECTS, '--since', 'yesterday-ish'])

    def test_runs_as_a_script(self):
        r = subprocess.run([sys.executable, TOOL, '--projects-dir', PROJECTS], capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b'sess-main-1', r.stdout)

    def test_price_table_is_labelled_as_list_prices(self):
        with open(TOOL, encoding='utf-8') as fh:
            source = fh.read()
        self.assertIn('list prices at time of writing, check current pricing', source)


if __name__ == '__main__':
    unittest.main()
