import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from baton.errors import ProjectError
from baton.retro_summary import CAPACITIES, INPUT_BUDGET, NOTE_BUDGET, existing_report, run_summary, scan_sources, synthesize
import test_retro_summary as fixtures


PROVIDER = r'''import json, pathlib, sys, time
root = pathlib.Path(ROOT)
prompt = sys.stdin.read()
mapping = pathlib.Path('sources.json').read_text()
manifest = json.loads(mapping)
inputs = {source['input']: pathlib.Path(source['input']).read_text() for source in manifest['sources']}
total = len(prompt.encode())
assert total <= 48 * 1024, total
assert mapping in prompt
assert all(text in prompt for text in inputs.values())
assert 'do not use tools' in prompt.lower() or 'use no tools' in prompt.lower()
intermediate = 'This is an intermediate evidence analysis' in prompt
sources = manifest['sources']
output = 'Analysis for ' + ', '.join(source['project'] for source in sources) + '.\n'
selected = sources[:1] if BEHAVIOR == 'missing' and intermediate else sources
output += '\n'.join('Concrete incident, impact and proposed fix [' + source['project'] + '](' + source['link'] + ').'
                    for source in selected)
if BEHAVIOR == 'unknown' and intermediate:
    output += '\nUnknown [project](../../unobserved/retro.md).'
if BEHAVIOR == 'excessive' and intermediate:
    output += '\n' + 'x' * (8 * 1024)
if BEHAVIOR == 'large-notes' and intermediate:
    output += '\n' + 'x' * 6600
if BEHAVIOR == 'malformed' and intermediate:
    output = 'BATON_RETRO_DRAFT_BEGIN\n' + output
record = {'sources': sources, 'inputs': inputs, 'bytes': total, 'intermediate': intermediate,
          'output': output + '\n', 'prompt': prompt}
with (root / 'summary-calls.jsonl').open('a') as handle:
    handle.write(json.dumps(record) + '\n')
if BEHAVIOR == 'slow':
    call_count = len((root / 'summary-calls.jsonl').read_text().splitlines())
    time.sleep(2 if call_count == 1 else 30)
print('BATON_RETRO_DRAFT_BEGIN')
print(output)
print('BATON_RETRO_DRAFT_END')
'''


class LargeSummaryTest(unittest.TestCase):
    setUp = fixtures.SummaryTest.setUp
    run_command = fixtures.SummaryTest.run_command
    git = fixtures.SummaryTest.git
    cli = fixtures.SummaryTest.cli
    commit_retro = fixtures.SummaryTest.commit_retro
    now = fixtures.SummaryTest.now
    manager = fixtures.SummaryTest.manager
    environment = fixtures.SummaryTest.environment
    provider = fixtures.SummaryTest.provider

    def create_sources(self) -> dict[str, bytes]:
        expected = {}
        for name in ('alpha', 'bravo', 'charlie', '日本語'):
            text = '# Retro: ' + name + '\n' + ('Concrete incident 日本語; impact and fix.\n' * 400)
            path = self.commit_retro(name, text)
            expected[name + '/retro.md'] = text.encode()
            if name == '日本語':
                path.write_text('DIRTY WORKING COPY MUST NOT ENTER THE ANALYSIS')
        self.assertGreater(sum(map(len, expected.values())), INPUT_BUDGET)
        return expected

    def provider_code(self, behavior: str = 'valid') -> str:
        return f'ROOT = {str(self.root)!r}\nBEHAVIOR = {behavior!r}\n' + PROVIDER

    def calls(self) -> list[dict]:
        path = self.root / 'summary-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_complete_committed_unicode_evidence_is_batched_and_all_notes_reach_final(self) -> None:
        expected = self.create_sources()
        state = self.root / 'private-state'
        state.mkdir()
        with self.environment(), self.provider(self.provider_code()):
            manifest = scan_sources(self.artifacts, now=self.now)
            result = synthesize(manifest, 'claude', state, 10)
        calls = self.calls()
        analyses = [call for call in calls if call['intermediate']]
        self.assertGreaterEqual(len(analyses), 2)
        self.assertEqual(len(calls), len(analyses) + 1)
        observed = {}
        for call in analyses:
            self.assertLessEqual(call['bytes'], INPUT_BUDGET)
            for source in call['sources']:
                self.assertEqual(source['kind'], 'retrospective')
                self.assertNotIn(source['path'], observed)
                observed[source['path']] = call['inputs'][source['input']].encode()
            self.assertLessEqual(len(call['output'].encode()), NOTE_BUDGET)
        self.assertEqual(observed, expected)
        final = calls[-1]
        self.assertFalse(final['intermediate'])
        self.assertLessEqual(final['bytes'], INPUT_BUDGET)
        self.assertEqual(set(final['inputs'].values()), {call['output'] for call in analyses})
        self.assertEqual({source['path'] for source in final['sources']}, set(expected))
        self.assertTrue(all(source['kind'] == 'analysis' for source in final['sources']))
        self.assertIn('count distinct projects, never batches', final['prompt'])
        for source in manifest['sources']:
            self.assertIn(source['link'], result)
        self.assertTrue(next(source for source in manifest['sources'] if source['project'] == '日本語')['dirty'])

    def test_intermediate_citations_and_markers_are_validated_before_continuing(self) -> None:
        self.create_sources()
        for behavior, message in (('missing', 'omitted a source'), ('unknown', 'unknown source'),
                                  ('malformed', 'ambiguous')):
            with self.subTest(behavior=behavior):
                state = self.root / behavior
                state.mkdir()
                record = self.root / 'summary-calls.jsonl'
                record.unlink(missing_ok=True)
                with self.environment(), self.provider(self.provider_code(behavior)):
                    with self.assertRaisesRegex(ProjectError, message):
                        synthesize(scan_sources(self.artifacts, now=self.now), 'claude', state, 10)
                self.assertEqual(len(self.calls()), 1)

    def test_excessive_intermediate_analysis_is_rejected(self) -> None:
        self.create_sources()
        state = self.root / 'private-state'
        state.mkdir()
        with self.environment(), self.provider(self.provider_code('excessive')):
            with self.assertRaisesRegex(ProjectError, 'analysis budget'):
                synthesize(scan_sources(self.artifacts, now=self.now), 'claude', state, 10)
        self.assertEqual(len(self.calls()), 1)

    def test_all_batches_share_one_timeout(self) -> None:
        self.create_sources()
        state = self.root / 'private-state'
        state.mkdir()
        with self.environment(), self.provider(self.provider_code('slow')):
            manifest = scan_sources(self.artifacts, now=self.now)
            started = time.monotonic()
            with self.assertRaisesRegex(ProjectError, 'exceeded 4 seconds'):
                synthesize(manifest, 'claude', state, 4)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.5)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(json.loads((state / 'process.json').read_text())['status'], 'finished')

    def test_one_complete_source_that_cannot_fit_never_launches(self) -> None:
        self.commit_retro('one-large-source', 'A' * INPUT_BUDGET)
        state = self.root / 'private-state'
        state.mkdir()
        with self.environment(), self.provider(self.provider_code()):
            with self.assertRaisesRegex(ProjectError, 'One retrospective or analysis exceeds'):
                synthesize(scan_sources(self.artifacts, now=self.now), 'claude', state, 10)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(state.glob('attempt-*.log')), [])

    def test_large_notes_are_combined_in_further_bounded_passes(self) -> None:
        for index in range(18):
            self.commit_retro(f'project-{index:02}', 'Complete original evidence.\n' * 700)
        state = self.root / 'private-state'
        state.mkdir()
        with self.environment(), self.provider(self.provider_code('large-notes')):
            manifest = scan_sources(self.artifacts, now=self.now)
            result = synthesize(manifest, 'claude', state, 20)
        calls = self.calls()
        reductions = [call for call in calls if call['intermediate']
                      and all(source['kind'] == 'analysis' for source in call['sources'])]
        self.assertTrue(reductions)
        self.assertFalse(calls[-1]['intermediate'])
        self.assertTrue(all(call['bytes'] <= INPUT_BUDGET for call in calls))
        self.assertEqual({source['path'] for source in calls[-1]['sources']},
                         {source['path'] for source in manifest['sources']})
        for source in manifest['sources']:
            self.assertIn(source['link'], result)

    def test_nonshrinking_analysis_groups_fail_without_repeated_model_calls(self) -> None:
        for index in range(4):
            self.commit_retro(f'project-{index}', 'Complete original evidence.\n' * 250)
        state = self.root / 'private-state'
        state.mkdir()
        with self.environment(), self.provider(self.provider_code('large-notes')), patch.dict(CAPACITIES, claude=11000):
            with self.assertRaisesRegex(ProjectError, 'without dropping evidence'):
                synthesize(scan_sources(self.artifacts, now=self.now), 'claude', state, 10)
        self.assertEqual(len(self.calls()), 4)
        self.assertTrue(all(call['intermediate'] for call in self.calls()))

    def test_staged_publication_and_retries_keep_all_sources_and_reuse_verified_report(self) -> None:
        expected = self.create_sources()
        with self.environment(), self.provider(self.provider_code('missing')):
            with self.assertRaisesRegex(ProjectError, 'omitted a source'):
                run_summary(self.manager(), 'claude', 10, now=self.now)
        self.commit_retro('later', 'Later evidence must not enter the frozen week.')
        with self.environment(), self.provider(self.provider_code()):
            result = run_summary(self.manager(), 'claude', 10, now=self.now)
            count = len(self.calls())
            revision = self.git(self.artifacts, 'rev-parse', 'HEAD')
            again = run_summary(self.manager(), 'claude', 10, now=self.now)
        self.assertTrue(result['committed'])
        self.assertFalse(result['published'])
        self.assertEqual(result['sources'], again['sources'])
        self.assertEqual({source['path'] for source in result['sources']}, set(expected))
        self.assertEqual(len(self.calls()), count)
        self.assertEqual(self.git(self.artifacts, 'rev-parse', 'HEAD'), revision)
        self.assertIsNotNone(existing_report(Path(result['report']).parent))
        changed = self.git(self.artifacts, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD').splitlines()
        self.assertEqual(changed, ['retros/2026-W44/report.md', 'retros/2026-W44/sources.json'])
