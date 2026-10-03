"""Real local candidate scoring with mocked paid coding turns; no model requests."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from research_agent import experiment_feedback as feedback
from research_agent import experiment_iteration as iteration
from research_agent.experiment_retrieval import dump, sha
from research_agent.workbench import OfflineRouter, Workbench
from research_agent.workbench_store import Conflict, WorkbenchStore


BASE = "def rank(corpus, queries, top_k=10):\n    return [[corpus[-1]['id']] for query in queries]\n"
FIRST = "def rank(corpus, queries, top_k=10):\n    return [[doc['id'] for doc in corpus[:1]] for query in queries]\n"
SECOND = FIRST.replace('corpus[:1]', 'corpus[:2]')


class IterationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = WorkbenchStore(self.root / 'state.db')
        self.app = Workbench(self.store, OfflineRouter, lambda *args: None, self.root / 'traces', start_worker=False)
        self.addCleanup(self.app.close)
        self.executor = self.app.experiments; self.executor.work_root = self.root / 'workspaces'
        self.space = self.store.save_space({'name': 'bounded'})['id']
        self.chat = self.store.create_conversation(self.space, 'independent')['id']
        corpus = [dict(id=f'paper:{i}', title='Original source', section='method', content=f'original text {i}',
                       url='https://example.org/study', page=i + 1) for i in range(5)]
        questions = [dict(id=f'dev-{i}', category='direct', split='dev', query=f'development {i}',
                          gold=[dict(document='paper', ordinal=i)]) for i in range(3)]
        questions.append(dict(id='held_out_secret', category='semantic', split='held_out',
                              query='unseen final wording', gold=[dict(document='paper', ordinal=3)]))
        self.data = dict(corpus=corpus, questions=questions,
                         signals=[dict(text=q['query'], dense=[]) for q in questions],
                         features={'signals_hash': 'fixture'})
        self.calls, self.evaluations, self.prompts, self.starting_code = [], [], [], []
        self.metrics = [{'name': 'recall_at5', 'unit': 'percent', 'direction': 'higher', 'target_mode': 'delta', 'target': 100},
                        {'name': 'mrr', 'unit': 'ratio', 'direction': 'higher', 'target_mode': 'delta', 'target': 0}]

    def run_case(self, candidates, *, max_iterations=3, target=100, missing_usage=False,
                 interrupt_dev=False, interrupt_final=False, fail_coding=False, cancel=False, excess_tokens=False,
                 interrupt_checkpoint=False, tamper_between_rounds=False):
        self.metrics[0]['target'] = target
        self.candidates = candidates
        original_evaluate = feedback.evaluate
        original_save = self.executor.save
        interrupts = []

        def save(job, state):
            original_save(job, state)
            if interrupt_checkpoint and state.get('code_hash') and not state.get('development_selected') and not interrupts:
                interrupts.append('checkpoint')
                raise RuntimeError('crashed after host source checkpoint')
            rounds = state.get('iteration', {}).get('rounds', [])
            if tamper_between_rounds and len(rounds) == 2 and rounds[-1]['status'] == 'preparing':
                workspace = Path(self.run['contract']['workspace'])
                (workspace / 'corpus.json').write_text('[]', encoding='utf-8')

        def measured(workspace, data, *args):
            splits = [q['split'] for q in data['questions']]
            self.evaluations.append(splits)
            if interrupt_dev and splits == ['dev'] * 3 and not interrupts:
                interrupts.append('dev'); raise RuntimeError('crashed during reserved development measurement')
            if interrupt_final and len(self.evaluations) > 1 and 'held_out' in splits:
                raise RuntimeError('final measurement crash')
            def runner(args, root, **kw):
                process = subprocess.run([sys.executable, '-X', 'utf8', '-I', '-B', '-c', feedback.PREDICT],
                                         cwd=root, input=kw['stdin'], capture_output=True, text=True, encoding='utf-8', timeout=10)
                return dict(stdout=process.stdout, stderr=process.stderr, seconds=.1, exit_code=process.returncode,
                            termination='completed' if not process.returncode else 'nonzero_exit')
            with patch.object(feedback, 'sandbox_command', return_value=['fixture']):
                return original_evaluate(workspace, data, lambda: False, runner=runner)

        def coding(args, workspace, **kw):
            self.starting_code.append((workspace / 'ranker.py').read_text(encoding='utf-8'))
            self.calls.append({'timeout': kw['timeout'], 'command': args})
            self.prompts.append(kw['stdin'])
            for name in ('dev.json', 'dev-inputs.json', 'dev-feedback.json'):
                visible = (workspace / name).read_text(encoding='utf-8')
                self.assertNotIn('held_out_secret', visible)
                self.assertNotIn('unseen final wording', visible)
            self.assertNotIn('held_out_secret', kw['stdin'])
            self.assertNotIn('unseen final wording', kw['stdin'])
            submitted = candidates[len(self.calls) - 1]
            versions = submitted if isinstance(submitted, list) else [submitted]
            for name, code in zip(('candidate-first.json', 'candidate-revised.json'), versions):
                (workspace / 'ranker.py').write_text(code, encoding='utf-8')
                dump(workspace / name, {'code': code, 'code_hash': sha(code)})
            (workspace / 'proposal.md').write_text('HYPOTHESIS: independent next attempt; final results unknown.', encoding='utf-8')
            usage = {'input_tokens': 9999 if excess_tokens else 123, 'output_tokens': 7}
            event = {'type': 'turn.completed', **({} if missing_usage else {'usage': usage})}
            kw['event'](event)
            if cancel: self.store.cancel(self.space, self.run['job_id'])
            if fail_coding == 'crash': raise RuntimeError('host process lost before receipt')
            return dict(exit_code=-1 if fail_coding or cancel else 0,
                        termination='cancelled' if cancel else 'timeout' if fail_coding else 'completed',
                        seconds=10, tool_calls=2, stdout=json.dumps(event) + '\n', stderr='saved process diagnosis')

        with ExitStack() as stack:
            stack.enter_context(patch.object(feedback, 'BASELINE', BASE))
            stack.enter_context(patch.object(feedback, 'metric_contract', return_value=self.metrics))
            config = feedback.configuration()
            stack.enter_context(patch.object(feedback, 'configuration', return_value=config))
            stack.enter_context(patch.object(feedback, 'frozen_data', return_value=(self.data, 'fixture')))
            stack.enter_context(patch.object(feedback, 'prepare_data', side_effect=lambda data, cancelled: copy.deepcopy(data)))
            stack.enter_context(patch.object(feedback, 'evaluate', side_effect=measured))
            stack.enter_context(patch('research_agent.experiments.run_process', side_effect=coding))
            stack.enter_context(patch.object(self.executor, 'save', side_effect=save))
            stack.enter_context(patch('research_agent.experiments.codex_path', return_value='codex.exe'))
            stack.enter_context(patch('research_agent.experiment_process.codex_path', return_value='codex.exe'))
            stack.enter_context(patch.dict('os.environ', {'SUDOCODE_API_KEY': 'test-placeholder'}))
            plan = self.executor.prepare(self.space, {'hypothesis': 'Improve only development failures'})
            self.run = self.executor.submit(self.space, plan['versions'][-1]['id'],
                dict(conversation_id=self.chat, authorize_execution=True, seconds=90, token_budget=9000,
                     max_iterations=max_iterations))
            self.executor.execute(self.store.claim_next())
            failed = self.executor.get(self.space, self.run['job_id'])
            if interrupt_dev or fail_coding or excess_tokens or interrupt_checkpoint:
                self.assertEqual(failed['job']['status'], 'failed', failed['state'])
                self.app.resume(self.space, self.run['job_id'])
                self.executor.execute(self.store.claim_next())
            if interrupt_final:
                self.assertEqual(failed['job']['status'], 'failed')
                count = len(self.evaluations)
                with self.assertRaisesRegex(Conflict, '不自动重放'):
                    self.app.resume(self.space, self.run['job_id'])
                self.assertEqual(len(self.evaluations), count, 'final test must not be replayed')
        return self.executor.get(self.space, self.run['job_id'])

    def test_three_independent_rounds_keep_best_development_source_and_one_final(self):
        result = self.run_case([FIRST, SECOND, BASE + '# regression\n'])
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        state = result['state']; loop = state['iteration']
        self.assertEqual(loop['stop_reason'], 'max_iterations')
        self.assertEqual(state['selected_label'], 'round-2-candidate-1')
        self.assertEqual(self.starting_code, [BASE, FIRST, SECOND])
        self.assertEqual(loop['totals']['tokens'], 390)
        self.assertEqual(loop['totals']['seconds'], 30)
        self.assertEqual(state['coding_invocations'], 3)
        self.assertEqual(len(state['usage']), 3)
        self.assertEqual(self.evaluations, [['dev'] * 3 + ['held_out'], ['dev'] * 3, ['dev'] * 3, ['dev'] * 3, ['dev'] * 3 + ['held_out']])
        self.assertEqual(self.executor.artifact(self.space, self.run['job_id'], 'ranker.py').read_text(encoding='utf-8'), SECOND)
        self.assertTrue(self.executor.artifact(self.space, self.run['job_id'], 'round-two-codex.json').is_file())
        final = json.loads(self.executor.artifact(self.space, self.run['job_id'], 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(final['selected_code_hash'], sha(SECOND))
        self.assertEqual(final['comparison']['right']['content']['config']['code_revision'], sha(SECOND))
        self.assertEqual([row['timeout'] for row in self.calls], [90, 80, 70])
        self.assertTrue(all(f'features.rollout_budget.limit_tokens={budget}' in row['command']
                            for row, budget in zip(self.calls, [9000, 8870, 8740])))

    def test_no_improvement_stops_after_two_rounds(self):
        result = self.run_case([BASE + '# one\n', BASE + '# two\n'])
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        self.assertEqual(result['state']['iteration']['stop_reason'], 'no_improvement')
        self.assertEqual(result['state']['selected_label'], 'baseline')
        self.assertEqual(len(self.calls), 2)

    def test_development_target_stops_before_more_coding(self):
        result = self.run_case([FIRST], target=1)
        self.assertEqual(result['state']['iteration']['stop_reason'], 'development_target_met')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['state']['candidate']['metrics']['recall_at5'], 0, 'test failure cannot trigger another turn')

    def test_missing_usage_stops_further_paid_calls_and_remains_unknown(self):
        result = self.run_case([FIRST], missing_usage=True)
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        self.assertEqual(result['state']['iteration']['stop_reason'], 'usage_incomplete')
        self.assertFalse(result['state']['iteration']['totals']['complete'])
        self.assertIsNone(result['state']['iteration']['rounds'][0]['accounting']['tokens'])
        self.assertEqual(len(self.calls), 1)

    def test_restart_during_development_reuses_receipt_then_only_new_round_runs(self):
        result = self.run_case([FIRST, SECOND], max_iterations=2, interrupt_dev=True)
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['state']['development']['trials'][0]['measurement_status'], 'interrupted')
        self.assertEqual(result['state']['iteration']['totals']['tokens'], 260)
        self.assertEqual(result['state']['selected_label'], 'round-2-candidate-1')

    def test_restart_while_measuring_first_snapshot_preserves_distinct_submitted_source(self):
        # The final coding submission is SECOND. The host temporarily installs FIRST
        # for development scoring before crashing; it must not overwrite that receipt.
        result = self.run_case([[FIRST, SECOND]], target=1, interrupt_dev=True)
        submitted = self.executor.artifact(self.space, self.run['job_id'], 'round-one-submitted-ranker.py')
        self.assertEqual(submitted.read_text(encoding='utf-8'), SECOND)
        self.assertEqual(result['job']['status'], 'completed', result['state'].get('failure'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['state']['development']['trials'][0]['measurement_status'], 'interrupted')
        self.assertEqual(result['state']['development']['trials'][1]['measurement_status'], 'completed')
        self.assertEqual(result['state']['selected_label'], 'round-1-candidate-2')
        self.assertEqual(result['state']['code_hash'], sha(SECOND))

    def test_failed_coding_resume_freezes_prior_best_without_spending_again(self):
        result = self.run_case([FIRST], fail_coding=True)
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        self.assertEqual(result['state']['iteration']['stop_reason'], 'coding_interrupted')
        self.assertEqual(result['state']['selected_label'], 'baseline')
        self.assertEqual(len(self.calls), 1)
        receipt = json.loads(self.executor.artifact(self.space, self.run['job_id'], 'round-one-codex.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['termination'], 'timeout')

    def test_missing_process_receipt_is_reserved_and_never_relaunched(self):
        result = self.run_case([FIRST], fail_coding='crash')
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['state']['iteration']['stop_reason'], 'coding_interrupted')
        self.assertFalse(result['state']['iteration']['totals']['complete'])
        self.assertEqual(result['state']['selected_label'], 'baseline')

    def test_successful_receipt_survives_source_checkpoint_restart_without_relaunch(self):
        result = self.run_case([FIRST], target=1, interrupt_checkpoint=True)
        self.assertEqual(result['job']['status'], 'completed', result['state'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['state']['selected_label'], 'round-1-candidate-1')
        self.assertEqual(result['state']['iteration']['totals']['tokens'], 130)

    def test_next_round_cannot_bless_a_changed_corpus(self):
        result = self.run_case([FIRST], tamper_between_rounds=True)
        self.assertEqual(result['job']['status'], 'failed')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('轮次之间冻结资料发生变化', result['state']['failure']['error'])
        self.assertIsNone(result['result_version_id'])

    def test_exceeded_usage_cannot_launch_another_turn_on_resume(self):
        result = self.run_case([FIRST], excess_tokens=True)
        self.assertEqual(result['state']['iteration']['stop_reason'], 'budget_exceeded')
        self.assertEqual(result['state']['selected_label'], 'baseline')
        self.assertEqual(len(self.calls), 1)

    def test_final_failure_is_not_replayed_or_used_for_new_coding(self):
        result = self.run_case([FIRST], target=1, interrupt_final=True)
        self.assertEqual(result['job']['status'], 'failed')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['state']['final_evaluation_status'], 'started')
        self.assertIn('final measurement crash', result['state']['failure']['error'])

    def test_cancellation_keeps_receipt_and_never_publishes_or_resumes(self):
        result = self.run_case([FIRST], cancel=True)
        self.assertEqual(result['job']['status'], 'cancelled')
        self.assertIsNone(result['result_version_id'])
        with self.assertRaises(Conflict): self.app.resume(self.space, self.run['job_id'])
        saved = json.loads(self.executor.artifact(self.space, self.run['job_id'], 'iterations.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['stop_reason'], 'cancelled')
        self.assertEqual(saved['rounds'][0]['accounting']['tokens'], 130)
        self.assertEqual(len(self.evaluations), 1)

    def test_validation_and_artifact_mapping(self):
        plan = self.executor.prepare(self.space, {'hypothesis': 'validation'})
        with patch('research_agent.experiments.codex_path', return_value='codex.exe'):
            for value in (True, 0, 4, '3'):
                with self.assertRaises(ValueError):
                    self.executor.submit(self.space, plan['versions'][-1]['id'],
                        dict(conversation_id=self.chat, authorize_execution=True, max_iterations=value))
        self.assertIsNone(iteration.artifact_name('../codex.json'))
        self.assertIsNone(iteration.artifact_name('round-four-codex.json'))
        self.assertIsNone(iteration.artifact_name('round-one-private.json'))


if __name__ == '__main__': unittest.main()
