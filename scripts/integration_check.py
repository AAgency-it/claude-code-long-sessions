#!/usr/bin/env python3
"""End-to-end check of the memory hook with a real (cheap) Claude Code session.

What it does, in a temporary folder, with `claude -p --model haiku --settings <temporary file>`:

  1. asks the session to write its state file                      -> the hook must link it to the session
  2. appends a random VERIFICATION-CODE to that file, from outside   -> the session cannot know it
  3. runs /compact in the same session                               -> the hook must put the file back
  4. asks the session for the code, forbidding tools                 -> it can only know it from the hook

PASS means the whole chain works: hook configuration, state file link, and re-injection after compaction.
The settings file is passed with --settings, so your own ~/.claude/settings.json is never touched.

It makes 3 short calls to the Haiku model (a few cents on an API key, or a few messages on a subscription).
Usage:
  python scripts/integration_check.py [--claude claude] [--model haiku] [--window 100000] [--keep] [--dry-run]
"""
import argparse
import glob
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, 'hooks', 'memory_hook.py')


def fwd(p):
    return str(p).replace('\\', '/')


def hook_command(python, hook_path, command):
    return f'"{fwd(python)}" "{fwd(hook_path)}" {command}'


def build_settings(hook_path, state_dir, data_dir, window=None, python=None):
    """The settings passed with --settings: the four memory hooks and the folders they use."""
    python = python or sys.executable

    def entry(command, timeout=10, matcher=None):
        e = {'hooks': [{'type': 'command', 'command': hook_command(python, hook_path, command), 'timeout': timeout}]}
        if matcher:
            e['matcher'] = matcher
        return [e]

    env = {'MEMORY_STATE_DIR': fwd(state_dir), 'MEMORY_DATA_DIR': fwd(data_dir)}
    if window:
        env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] = str(window)
    return {
        'env': env,
        'hooks': {
            'PostToolUse': entry('after-tool'),
            'PreCompact': entry('pre-compact'),
            'PostCompact': entry('post-summary'),
            'SessionStart': entry('after-compact', timeout=15, matcher='compact'),
        },
    }


def project_claude_md(root):
    """A CLAUDE.md for the temporary project: the compact-instructions template shipped with the kit."""
    with open(os.path.join(root, 'templates', 'CLAUDE.compact-instructions.md'), encoding='utf-8') as fh:
        return '# Temporary project for the memory hook check\n\n' + fh.read()


def new_code():
    return 'CHECK-' + secrets.token_hex(4).upper()


def parse_result(stdout):
    """(session_id, result text) from the JSON that `claude -p --output-format json` prints."""
    for chunk in [stdout] + stdout.strip().splitlines()[::-1]:
        try:
            o = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(o, dict):
            return o.get('session_id'), o.get('result') or ''
    return None, ''


class Check:
    def __init__(self, args):
        self.args = args
        self.tmp = tempfile.mkdtemp(prefix='long-sessions-check-')
        self.project = os.path.join(self.tmp, 'project')
        self.state_dir = os.path.join(self.project, 'session-state')
        self.data_dir = os.path.join(self.tmp, 'data')
        self.settings_file = os.path.join(self.tmp, 'settings.json')
        self.failed = []
        os.makedirs(self.state_dir)
        with open(self.settings_file, 'w', encoding='utf-8') as fh:
            json.dump(build_settings(HOOK, self.state_dir, self.data_dir, self.args.window), fh, indent=1)
        with open(os.path.join(self.project, 'CLAUDE.md'), 'w', encoding='utf-8') as fh:
            fh.write(project_claude_md(ROOT))

    def claude(self, prompt, resume=None):
        cmd = [self.args.claude, '-p', '--model', self.args.model, '--settings', self.settings_file,
               '--output-format', 'json', '--permission-mode', 'acceptEdits']
        if resume:
            cmd += ['--resume', resume]
        cmd.append(prompt)
        if self.args.dry_run:
            print('  would run:', ' '.join(cmd))
            return None, ''
        r = subprocess.run(cmd, cwd=self.project, capture_output=True, text=True, encoding='utf-8', timeout=600)
        sid, text = parse_result(r.stdout)
        if r.returncode != 0 and not sid:
            print('  claude failed:', (r.stderr or r.stdout)[:500])
        return sid, text

    def verdict(self, name, ok, detail=''):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''))
        if not ok:
            self.failed.append(name)

    def session_records(self):
        out = []
        for f in glob.glob(os.path.join(self.data_dir, '*.json')):
            try:
                with open(f, encoding='utf-8') as fh:
                    out.append(json.load(fh))
            except ValueError:
                pass
        return out

    def run(self):
        state_file = os.path.join(self.state_dir, 'check.md')
        code = new_code()
        print(f'Temporary folder: {self.tmp}')

        print('1. The session writes its state file')
        sid, _ = self.claude(
            f'Create the file {fwd(state_file)} with the Write tool. It must contain a heading "# Next step" '
            'followed by the line "Run the check". Then answer with the single word DONE.')
        if self.args.dry_run:
            shutil.rmtree(self.tmp, ignore_errors=True)
            return 0
        self.verdict('state file exists', os.path.exists(state_file))
        linked = [d for d in self.session_records() if os.path.normcase(os.path.normpath(d.get('state', ''))) ==
                  os.path.normcase(os.path.normpath(state_file))]
        self.verdict('hook linked the state file to the session', bool(linked))
        if not os.path.exists(state_file):
            return self.finish()

        print('2. A verification code is added from outside')
        with open(state_file, 'a', encoding='utf-8') as fh:
            fh.write(f'\nVERIFICATION-CODE: {code}\n')

        print('3. /compact in the same session')
        sid2, _ = self.claude('/compact', resume=sid)
        compacted = [d for d in self.session_records() if d.get('compactions')]
        self.verdict('hook saw the compaction', bool(compacted))
        if compacted:
            last = compacted[0]['compactions'][-1]
            print(f"     trigger={last.get('trigger')} summary_mentions_state={last.get('summary_mentions_state')}")

        print('4. The session is asked for the code, without tools')
        _, answer = self.claude(
            'What is the VERIFICATION-CODE in your state file? Answer with the code only. '
            'Do not use any tool and do not read any file: tell me only what you already know.', resume=sid2 or sid)
        self.verdict('the code survived the compaction', code in answer, f'answer: {answer.strip()[:80]!r}')
        return self.finish()

    def finish(self):
        print()
        print('RESULT:', 'PASS' if not self.failed else 'FAIL: ' + ', '.join(self.failed))
        if self.args.keep or self.failed:
            print('Files kept in', self.tmp)
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return 1 if self.failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='End-to-end check of the memory hook with claude -p.')
    ap.add_argument('--claude', default='claude', help='the claude executable (default: claude)')
    ap.add_argument('--model', default='haiku', help='model for the check (default: haiku)')
    ap.add_argument('--window', type=int, default=None, help='CLAUDE_CODE_AUTO_COMPACT_WINDOW for the check')
    ap.add_argument('--keep', action='store_true', help='keep the temporary folder')
    ap.add_argument('--dry-run', action='store_true', help='print the commands without running claude')
    args = ap.parse_args(argv)
    found = shutil.which(args.claude)                  # also finds claude.cmd from an npm install on Windows
    if found:
        args.claude = found
    elif not args.dry_run:
        print(f'Cannot find the Claude Code executable {args.claude!r}: install it, or pass --claude <path>.')
        return 2
    return Check(args).run()


if __name__ == '__main__':
    sys.exit(main())
