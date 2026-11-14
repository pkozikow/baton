import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from baton.doctor_smoke import model_checks
from baton.providers import scrubbed_environment
from provider_fixtures import EXTRA, ROOT, registered


FAKE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

args = sys.argv[1:]
provider = Path(sys.argv[0]).name
if args[-3:] == ['mcp', 'list', '--json']:
    print('[]')
    raise SystemExit(0)
if provider == 'agent' and '--trust' not in args:
    print('Workspace Trust Required', file=sys.stderr)
    raise SystemExit(1)
with open(os.environ['DOCTOR_RECORD'], 'a') as handle:
    handle.write(json.dumps({'provider': provider, 'args': args, 'cwd': os.getcwd(),
                             'session': os.environ.get('CODEX_THREAD_ID')}) + '\n')
if provider == 'agy':
    message = json.loads(sys.stdin.read())
    assert message['event'] == 'user'
    assert '--print' not in args and '--add-dir' not in args
    text = message['message']['content']
elif provider == 'claude':
    text = args[-1]
else:
    text = sys.stdin.read()
nonce = re.search(r'BATON_DOCTOR_[a-f0-9]+', text).group()
behavior = os.environ.get('DOCTOR_BEHAVIOR', '')
if behavior == 'timeout' and provider == 'codex':
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
    Path(os.environ['DOCTOR_CHILD']).write_text(str(child.pid))
    time.sleep(60)
if behavior == 'budget' and provider == 'codex':
    time.sleep(1.2)
if behavior == 'reject_xhigh' and 'model_reasoning_effort="xhigh"' in args:
    print('unsupported reasoning effort', file=sys.stderr)
    raise SystemExit(7)
if behavior == 'wrong':
    nonce = 'wrong output'
if behavior == 'echo':
    nonce = text
if '--output-last-message' in args:
    Path(args[args.index('--output-last-message') + 1]).write_text(nonce)
elif provider == 'agy':
    print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': nonce}}))
else:
    print(nonce)
if behavior == 'nonzero' or (behavior == 'budget' and provider == 'codex'):
    print('model does not exist; token=private-secret', file=sys.stderr)
    raise SystemExit(7)
'''


class DoctorSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.record = self.root / 'calls.jsonl'
        for binary in ('codex', 'claude', 'agy', 'agent'):
            path = self.root / binary
            path.write_text(FAKE)
            path.chmod(0o755)
        env = dict(os.environ)
        for key in ('CODEX_BIN', 'CLAUDE_BIN', 'AGY_BIN', 'CURSOR_BIN', 'CODEX_HOME',
                    'CODEX_REASONING_EFFORT', 'PEER_REVIEW_CODEX_REASONING', 'PEER_REVIEW_CODEX_AUTO_FALLBACK',
                    'DOCTOR_BEHAVIOR'):
            env.pop(key, None)
        env.update({name.upper() + '_BIN': str(self.root / binary)
                    for name, binary in [('codex', 'codex'), ('claude', 'claude'), ('agy', 'agy'), ('cursor', 'agent')]})
        env.update(DOCTOR_RECORD=str(self.record), CODEX_HOME=str(self.root / 'codex-home'),
                   PEER_REVIEW_CODEX_AUTO_FALLBACK='0', DOCTOR_CHILD=str(self.root / 'child.pid'))
        self.environ = patch.dict(os.environ, env, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_defaults_use_actual_adapters_without_model_flags(self) -> None:
        os.environ['CODEX_MODEL'] = 'ignored-environment-model'
        checks = model_checks({})
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        calls = {call['provider']: call for call in self.calls()}
        self.assertEqual(set(calls), {'codex', 'claude', 'agy'})
        for call in calls.values():
            self.assertNotIn('--model', call['args'])
            self.assertFalse((Path(call['cwd']) / '.git').exists())
        self.assertEqual(calls['codex']['args'][0], 'exec')
        self.assertIn('--output-last-message', calls['codex']['args'])
        self.assertEqual(calls['claude']['args'][:3], ['-p', '--restricted', '--strict-mcp-config'])
        self.assertIn('--print-timeout', calls['agy']['args'])
        self.assertEqual(calls['agy']['args'][4:8], ['--input-format', 'stream-json', '--output-format', 'stream-json'])
        self.assertIn('--sandbox', calls['agy']['args'])
        self.assertNotIn('--print', calls['agy']['args'])
        self.assertFalse(Path(calls['agy']['cwd']).exists())

    def test_all_configured_literals_and_tier_targets_are_deduplicated(self) -> None:
        config = {'plan_review_model': {'codex': 'strong', 'claude': 'opus', 'agy': 'gemini', 'cursor': 'composer'},
                  'implementation_review_model': {'codex': 'astra', 'claude': 'opus', 'agy': 'gemini', 'cursor': 'composer'},
                  'model_tiers': {'codex': {'strong': 'astra', 'weak': 'luna'}, 'cursor': {'medium': 'composer'}}}
        checks = model_checks(config)
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        cases = [(call['provider'], call['args'][call['args'].index('--model') + 1]) for call in self.calls()]
        self.assertCountEqual(cases, [('codex', 'astra'), ('codex', 'luna'), ('claude', 'opus'),
                                     ('agy', 'gemini'), ('agent', 'composer')])
        cursor = next(call for call in self.calls() if call['provider'] == 'agent')
        self.assertEqual(cursor['args'][:5], ['--print', '--output-format', 'text', '--mode', 'ask'])
        self.assertIn('--trust', cursor['args'])
        self.assertNotIn('--force', cursor['args'])
        self.assertNotIn('--yolo', cursor['args'])

    def test_tier_targets_run_with_their_effort_and_without_codex_fallback(self) -> None:
        config = {'plan_review_model': {'codex': 'strong', 'claude': 'opus', 'agy': 'weak'},
                  'implementation_review_model': {'codex': 'astra', 'claude': 'opus', 'agy': 'weak'},
                  'model_tiers': {'codex': {'strong': 'astra', 'medium': 'astra', 'weak': 'astra'},
                                  'claude': {'strong': 'opus'}, 'agy': {'weak': 'gemini'}},
                  'model_tier_effort': {'codex': {'medium': 'xhigh'}, 'claude': {'strong': 'max'}}}
        # Per-run review settings, even invalid ones, neither replace a tier's effort nor hide its rejection.
        os.environ.update(PEER_REVIEW_CODEX_AUTO_FALLBACK='2', PEER_REVIEW_CODEX_REASONING='bogus',
                          DOCTOR_BEHAVIOR='reject_xhigh')
        checks = model_checks(config)

        def effort(call: dict) -> str | None:
            args = call['args']
            if call['provider'] == 'codex':
                return args[args.index('--config') + 1].split('"')[1]
            return args[args.index('--effort') + 1] if '--effort' in args else None

        # Tiers sharing a model are one case per effort; a literal selection of a tier target adds none.
        self.assertCountEqual([(call['provider'], effort(call)) for call in self.calls()],
                              [('codex', 'high'), ('codex', 'xhigh'), ('claude', 'max'), ('agy', 'high')])
        failed = [check for check in checks if check['status'] != 'pass']
        self.assertEqual([check['name'] for check in failed], ['model:codex:astra@xhigh'])
        self.assertIn("'astra' at reasoning effort xhigh", failed[0]['message'])
        self.assertCountEqual([check['name'] for check in checks if check['status'] == 'pass'],
                              ['models:configuration', 'model:codex:astra@high', 'model:claude:opus@max',
                               'model:agy:gemini@high'])

    def test_cursor_mapping_alone_opts_it_in(self) -> None:
        checks = model_checks({'model_tiers': {'cursor': {'weak': 'composer'}}})
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        cursor = [call for call in self.calls() if call['provider'] == 'agent']
        self.assertEqual(len(cursor), 1)
        self.assertEqual(cursor[0]['args'][-2:], ['--model', 'composer'])

    def test_nondefault_workers_smoke_test_their_models_without_review_enablement(self) -> None:
        config = {'worker_model': {'codex': 'worker-literal', 'cursor': 'medium'},
                  'model_tiers': {'cursor': {'medium': 'cursor-worker'}}}
        checks = model_checks(config)
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        cases = [(call['provider'], call['args'][call['args'].index('--model') + 1]
                  if '--model' in call['args'] else 'default') for call in self.calls()]
        self.assertCountEqual(cases, [('codex', 'default'), ('claude', 'default'), ('agy', 'default'),
                                     ('codex', 'worker-literal'), ('agent', 'cursor-worker')])

    def test_default_workers_do_not_enable_optin_commands_or_extra_smokes(self) -> None:
        os.environ['CURSOR_BIN'] = str(self.root / 'absent-agent')
        config = {'worker_model': {'codex': 'default', 'claude': 'default', 'agy': 'default', 'cursor': 'default'}}
        checks = model_checks(config)
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        self.assertCountEqual([call['provider'] for call in self.calls()], ['codex', 'claude', 'agy'])
        static = model_checks(config, smoke=False)
        self.assertNotIn('command:cursor', [check['name'] for check in static])

    def test_worker_and_review_target_is_only_smoked_once(self) -> None:
        config = {'worker_model': {'codex': 'shared-model'}, 'plan_review_model': {'codex': 'shared-model'}}
        checks = model_checks(config)
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        self.assertEqual(sum(call['args'].count('shared-model') for call in self.calls()), 1)

    def test_missing_mapping_or_invalid_schema_never_invokes_cli(self) -> None:
        for config in ({'plan_review_model': {'codex': 'strong'}},
                       {'worker_model': {'codex': 'strong'}}, {'worker_model': {'unknown': 'model'}},
                       {'model_tiers': {'codex': {'strong': 'default'}}},
                       {'implementation_review_model': {'unknown': 'model'}},
                       {'plan_review_model': {'agy': '\nmodel'}}, {'model_tiers': []}):
            with self.subTest(config=config):
                checks = model_checks(config)
                self.assertEqual(checks[0]['status'], 'fail')
        self.assertFalse(self.record.exists())

    def test_smoke_skip_still_checks_commands_and_models(self) -> None:
        os.environ['AGY_BIN'] = str(self.root / 'missing')
        checks = model_checks({}, smoke=False)
        self.assertEqual(next(check for check in checks if check['name'] == 'command:agy')['status'], 'fail')
        self.assertEqual(checks[-1]['status'], 'skip')
        self.assertFalse(self.record.exists())

    def test_missing_command_is_failure(self) -> None:
        os.environ['CODEX_BIN'] = str(self.root / 'missing')
        checks = model_checks({})
        self.assertEqual(next(check for check in checks if check['name'] == 'model:codex:default')['status'], 'fail')
        self.assertEqual(next(check for check in checks if check['name'] == 'model:agy:default')['status'], 'pass')

    def test_nonzero_with_output_fails_including_codex_and_redacts_credentials(self) -> None:
        os.environ['DOCTOR_BEHAVIOR'] = 'nonzero'
        checks = model_checks({})
        for check in checks[1:]:
            self.assertEqual(check['status'], 'fail', check)
            self.assertNotIn('private-secret', check.get('details', ''))
            self.assertIn('exit 7', check['message'])
        self.assertEqual(len(self.calls()), 3)

    def test_wrong_response_fails(self) -> None:
        os.environ['DOCTOR_BEHAVIOR'] = 'wrong'
        checks = model_checks({})
        self.assertTrue(all(check['status'] == 'fail' for check in checks[1:]), checks)

    def test_timeout_reaps_cli_process_group_without_fallback(self) -> None:
        os.environ['DOCTOR_BEHAVIOR'] = 'timeout'
        os.environ['PEER_REVIEW_CODEX_AUTO_FALLBACK'] = '1'
        started = time.monotonic()
        # Long enough for the stub to start under parallel test load, when exec can take seconds.
        checks = model_checks({}, timeout=5)
        elapsed = time.monotonic() - started
        codex = next(check for check in checks if check['name'] == 'model:codex:default')
        self.assertEqual(codex['status'], 'fail', checks)
        self.assertIn('exit 124', codex['message'])
        self.assertLess(elapsed, 15)
        child = int((self.root / 'child.pid').read_text())
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'), state)
        self.assertEqual(sum(call['provider'] == 'codex' for call in self.calls()), 1)

    def test_codex_reasoning_fallbacks_share_one_timeout_budget(self) -> None:
        os.environ['DOCTOR_BEHAVIOR'] = 'budget'
        os.environ['PEER_REVIEW_CODEX_AUTO_FALLBACK'] = '1'
        started = time.monotonic()
        # The budget also covers Codex's MCP server listing before the first attempt.
        checks = model_checks({}, timeout=3)
        elapsed = time.monotonic() - started
        codex = next(check for check in checks if check['name'] == 'model:codex:default')
        self.assertEqual(codex['status'], 'fail', checks)
        # The deadline either stops an attempt (124) or leaves no time for the next fallback, when the last
        # attempt's own failure (7) stands; either way all attempts fit in one budget.
        self.assertRegex(codex['message'], r'\(exit (124|7)\)')
        self.assertLess(elapsed, 4.5)
        self.assertGreater(sum(call['provider'] == 'codex' for call in self.calls()), 1)

    def test_relative_binary_override_resolves_before_entering_private_directory(self) -> None:
        os.environ['CODEX_BIN'] = os.path.relpath(self.root / 'codex')
        checks = model_checks({})
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)

    def test_symlink_preserves_the_invoked_harness_name(self) -> None:
        shim = self.root / 'shim'
        (self.root / 'codex').rename(shim)
        (self.root / 'codex').symlink_to(shim)
        checks = model_checks({})
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        self.assertIn('codex', {call['provider'] for call in self.calls()})

    def test_codex_settings_do_not_prevent_other_provider_smoke_tests(self) -> None:
        for variable in ('PEER_REVIEW_CODEX_REASONING', 'PEER_REVIEW_CODEX_AUTO_FALLBACK'):
            with self.subTest(variable=variable):
                os.environ[variable] = 'invalid'
                checks = model_checks({})
                states = {check['name']: check['status'] for check in checks}
                self.assertEqual(states['model:codex:default'], 'fail')
                self.assertEqual(states['model:claude:default'], 'pass')
                self.assertEqual(states['model:agy:default'], 'pass')
                os.environ.pop(variable)

    def test_echoed_prompt_does_not_count_as_a_model_response(self) -> None:
        os.environ['DOCTOR_BEHAVIOR'] = 'echo'
        checks = model_checks({})
        self.assertTrue(all(check['status'] == 'fail' for check in checks[1:]), checks)

    def test_sigterm_kills_reviewer_process_groups(self) -> None:
        os.environ['DOCTOR_BEHAVIOR'] = 'timeout'
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from baton.doctor_smoke import model_checks; '
                'model_checks({}, timeout=60)')
        process = subprocess.Popen([sys.executable, '-c', code, str(ROOT)], env=dict(os.environ),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            # The stub creates the file before writing the PID, so wait for its content.
            while time.monotonic() < deadline and not ((self.root / 'child.pid').exists()
                                                       and (self.root / 'child.pid').read_text().strip()):
                time.sleep(0.02)
            self.assertTrue((self.root / 'child.pid').read_text().strip())
            started = time.monotonic()
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, 124)
            self.assertLess(time.monotonic() - started, 5)
            child = int((self.root / 'child.pid').read_text())
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'), state)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_session_identity_is_not_inherited(self) -> None:
        os.environ['CODEX_THREAD_ID'] = 'owner-thread'
        self.assertNotIn('CODEX_THREAD_ID', scrubbed_environment())
        checks = model_checks({})
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        self.assertEqual({call['session'] for call in self.calls()}, {None})

    def test_registered_extra_provider_needs_no_other_code(self) -> None:
        (self.root / 'extra-agent').write_text(FAKE)
        (self.root / 'extra-agent').chmod(0o755)
        os.environ[EXTRA.binary_env] = str(self.root / 'extra-agent')
        with registered():
            checks = model_checks({'plan_review_model': {'extra': 'extra-model'},
                                   'implementation_review_model': {}})
        self.assertTrue(all(check['status'] == 'pass' for check in checks), checks)
        self.assertIn('model:extra:extra-model', [check['name'] for check in checks])
        extra = [call for call in self.calls() if call['provider'] == 'extra-agent']
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]['args'][:2], ['--model', 'extra-model'])
        self.assertIn('BATON_DOCTOR_', extra[0]['args'][2])

    def test_nonpositive_timeout_fails_without_cli(self) -> None:
        self.assertEqual(model_checks({}, timeout=0)[0]['status'], 'fail')
        self.assertFalse(self.record.exists())


if __name__ == '__main__':
    unittest.main()
