"""Real filesystem/SQLite tool lifecycle; external CLI replaced only in unit tests."""
import json
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research_agent.coding_tool import (CodingTool, audit_qasper_result, materialize_readonly_files,
                                         regular_file, reserved_resources, validate_request)
from research_agent.workbench_store import Conflict, WorkbenchStore
from research_agent.contracts import ModelDecision


class CodingToolTests(unittest.TestCase):
    def test_terminal_sequential_interruption_charges_only_one_unknown_call(self):
        request = {'seconds': 0, 'token_budget': 0, 'commands': [],
                   'model_requests': {'mode': 'stdio', 'max_requests': 23000, 'seconds': 86400}}
        state = {'model_requests': [{'elapsed_seconds': 4}, {'elapsed_seconds': 5}],
                 'model_request_started': 2}
        row = {'request': json.dumps(request), 'state': json.dumps(state), 'status': 'failed'}
        self.assertEqual(reserved_resources(row)['experiment_model_calls'], 3)
        # No final response exists: retain the time reservation and replay guard.
        self.assertEqual(reserved_resources(row)['experiment_model_seconds'], 86400)
        self.assertEqual(reserved_resources({**row, 'status': 'running'})['experiment_model_calls'], 23000)
        for invalid in (0, 3, True, '2'):
            with self.subTest(started=invalid):
                damaged = json.dumps({**state, 'model_request_started': invalid})
                self.assertEqual(reserved_resources({**row, 'state': damaged})['experiment_model_calls'], 23000)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = WorkbenchStore(self.root/'db.sqlite')
        self.space = self.store.save_space({'name': 'tool'})['id']
        self.chat = self.store.create_conversation(self.space, 'research')['id']
        self.store.message(self.space, self.chat, 'user', 'Implement and evaluate')
        job = self.store.enqueue(self.space, self.chat, 'goal', 'goal', [])
        self.job = self.store.claim_next()
        payload = {'auto_research': {'authorize_execution': True, 'budget': {
            'coding_calls': 3, 'coding_seconds': 90, 'coding_tokens': 6000, 'experiment_seconds': 30}}}
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE research_jobs SET kind='AUTO_RESEARCH',payload=? WHERE id=?", (json.dumps(payload), job['id']))
        self.tool = CodingTool(SimpleNamespace(store=self.store, trace_dir=self.root/'traces'))
        self.tool.work_root = self.root/'work'
        self.tool.output_root = self.root/'receipts'
        self.request = {'task': 'Implement proposed method', 'plan': 'Compare against baseline', 'seconds': 30,
                        'token_budget': 2000, 'commands': [{'name': 'baseline', 'role': 'baseline',
                        'script': 'evaluate.py', 'args': [], 'result_path': 'result.json', 'seconds': 10}]}

    def test_host_audits_qasper_recall_and_keeps_script_metrics(self):
        workspace = self.root / 'qasper'
        (workspace / 'data/qasper').mkdir(parents=True)
        (workspace / 'results').mkdir()
        task = {'id': 'q', 'source_id': 'paper', 'gold_context_ids': ['a', 'b']}
        (workspace / 'data/qasper/tasks.json').write_text(json.dumps({'tasks': [task]}), encoding='utf-8')
        (workspace / 'data/qasper/corpus.json').write_text(json.dumps([
            {'id': 'a', 'source_id': 'paper'}, {'id': 'b', 'source_id': 'paper'},
            {'id': 'x', 'source_id': 'paper'}]), encoding='utf-8')
        (workspace / 'results/rankings.json').write_text(json.dumps({'rankings': {
            '13': {'q': [{'paragraph_id': 'x'}, {'id': 'a'}]},
            '17': {'q': [{'id': 'x'}, {'id': 'a'}]}}}), encoding='utf-8')
        contract = {'name': 'qasper_fractional_recall_v1', 'tasks_path': 'data/qasper/tasks.json',
                    'corpus_path': 'data/qasper/corpus.json',
                    'tasks_sha256': hashlib.sha256((workspace / 'data/qasper/tasks.json').read_bytes()).hexdigest(),
                    'corpus_sha256': hashlib.sha256((workspace / 'data/qasper/corpus.json').read_bytes()).hexdigest()}
        result, audit = audit_qasper_result(workspace, {
            'metrics': {'recall_at_5': 1.0, 'mrr_at_5': .5},
            'config': {'dataset': 'qasper_frozen_24', 'seeds': [13, 17]},
            'diagnostics': {'ranking_file': str(workspace / 'results' / 'rankings.json')}}, contract)
        self.assertEqual(result['metrics']['recall_at_5'], .5)
        self.assertEqual(result['metrics']['hit_at_5'], 1.0)
        self.assertEqual(audit['reported_metrics']['recall_at_5'], 1.0)
        self.assertEqual(result['diagnostics']['metric_audit']['contract'], 'qasper_fractional_recall_v1')

        for invalid in (123, True, [], None):
            with self.subTest(metrics=invalid), self.assertRaises(ValueError):
                audit_qasper_result(workspace, {**result, 'metrics': invalid}, contract)

    def test_locomo_private_scoring_and_invalid_result_keep_measurement_receipts(self):
        workspace = self.tool.work_root / self.job['id']; workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('pass', encoding='utf-8')
        inputs = {'tasks': [{'id': 'q', 'conversation_id': 'c', 'split': 'development', 'category': 1}],
                  'corpus': [{'conversation_id': 'c', 'split': 'development', 'conversation': {
                      'session_1': [{'dia_id': 'D1:1'}]}}]}
        for name, value in inputs.items():
            (workspace / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
        (workspace / 'upstream').mkdir()
        (workspace / 'upstream/algorithm.py').write_text('ORIGINAL = True', encoding='utf-8')
        private = self.store.path.parent / 'auto-research' / self.job['id']; private.mkdir(parents=True)
        labels = private / 'scoring-labels.json'
        labels.write_text(json.dumps([{'id': 'q', 'answer': 'Luna', 'gold_evidence': ['D1:1'],
                                      'retrieval_scorable': True}]), encoding='utf-8')
        contract = {'name': 'locomo_qa_v1', 'seeds': [13], 'tasks_path': 'tasks.json', 'corpus_path': 'corpus.json',
                    'labels_sha256': hashlib.sha256(labels.read_bytes()).hexdigest(),
                    **{name + '_sha256': hashlib.sha256((workspace / (name + '.json')).read_bytes()).hexdigest()
                       for name in inputs}}
        from research_agent.coding_tool import protected_hash
        contract['frozen_sources'] = {'upstream': protected_hash(workspace, 'upstream')}
        with closing(self.store._connect()) as db, db:
            payload = json.loads(db.execute('SELECT payload FROM research_jobs WHERE id=?', (self.job['id'],)).fetchone()[0])
            payload['auto_research']['metric_contract'] = contract
            db.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json.dumps(payload), self.job['id']))
        for index, split in enumerate(('development', [])):
            def runner(*args, **kwargs):
                (workspace / 'predictions.jsonl').write_text(json.dumps({
                    'task_id': 'q', 'seed': 13, 'answer': 'Luna', 'evidence_ids': ['D1:1']}) + '\n', encoding='utf-8')
                (workspace / 'result.json').write_text(json.dumps({'metrics': {'answer_f1': 0},
                    'config': {'dataset': 'locomo', 'dataset_version': contract['tasks_sha256'], 'split': split, 'seeds': [13]},
                    'diagnostics': {'predictions_path': 'predictions.jsonl'}}), encoding='utf-8')
                return {'exit_code': 0, 'termination': 'completed', 'seconds': .1, 'stdout': 'preserve me', 'stderr': ''}
            task = self.tool.submit(self.job, f'locomo-{index}', {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0})
            self.assertIn('upstream', task['request']['protected_files'])
            with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
                 patch('research_agent.coding_tool.run_process', side_effect=runner):
                self.tool.execute(self.space, task['id'])
            result = self.tool.get(self.space, task['id'])
            measurement = result['state']['measurements'][0]
            self.assertEqual(measurement['valid'], index == 0)
            self.assertEqual(measurement['receipt']['stdout'], 'preserve me')
            self.assertTrue((self.tool.output_root / task['id'] / 'measurement-00.json').is_file())
            self.assertTrue((self.tool.output_root / task['id'] / 'prediction-00-predictions_path.jsonl').is_file())
            if index == 0:
                self.assertEqual(measurement['metrics']['answer_f1'], 1)
                self.assertEqual(measurement['metric_audit']['reported_metrics']['answer_f1'], 0)
                self.assertIn('predictions_path', measurement['prediction_artifacts'])
        (workspace / 'upstream/algorithm.py').write_text('ORIGINAL = False', encoding='utf-8')
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'changed-official-source', {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0})

    def test_host_rejects_unbounded_seed_expansion_before_ranking_parse(self):
        from research_agent.coding_tool import qasper_ranking_rows
        seeds = list(range(65))
        compact = json.dumps({'rankings': {str(seed): {'q': ['a']} for seed in seeds}})
        with self.assertRaises(ValueError):
            qasper_ranking_rows(compact, [{'id': 'q'}], seeds)

    def test_host_audits_jsonl_and_rejects_unbound_or_tampered_contract(self):
        workspace = self.root / 'qasper-jsonl'
        (workspace / 'data/qasper').mkdir(parents=True)
        (workspace / 'results').mkdir()
        task = {'id': 'q', 'source_id': 'paper', 'gold_context_ids': ['a', 'b']}
        (workspace / 'data/qasper/tasks.json').write_text(json.dumps({'tasks': [task]}), encoding='utf-8')
        (workspace / 'data/qasper/corpus.json').write_text(json.dumps([
            {'id': 'a', 'source_id': 'paper'}, {'id': 'b', 'source_id': 'paper'}]), encoding='utf-8')
        (workspace / 'results/rankings.jsonl').write_text(
            '\n'.join(json.dumps({'seed': seed, 'task_id': 'q', 'rankings': [{'paragraph_id': 'a'}]}) for seed in (13, 17)) + '\n',
            encoding='utf-8')
        contract = {'name': 'qasper_fractional_recall_v1', 'tasks_path': 'data/qasper/tasks.json',
                    'corpus_path': 'data/qasper/corpus.json',
                    'tasks_sha256': hashlib.sha256((workspace / 'data/qasper/tasks.json').read_bytes()).hexdigest(),
                    'corpus_sha256': hashlib.sha256((workspace / 'data/qasper/corpus.json').read_bytes()).hexdigest()}
        result, _ = audit_qasper_result(workspace, {
            'metrics': {'recall_at_5': .5, 'mrr_at_5': 1.0},
            'config': {'dataset': 'qasper_frozen_24', 'seeds': [13, 17]},
            'diagnostics': {'ranking_file': 'results\\rankings.jsonl'}}, contract)
        self.assertEqual(result['metrics']['recall_at_5'], .5)
        with self.assertRaises(ValueError):
            audit_qasper_result(workspace, {'metrics': {}, 'config': {'dataset': 'paper', 'seeds': [13, 17]},
                                            'diagnostics': {'ranking_file': 'results/rankings.jsonl'}}, contract)
        (workspace / 'data/qasper/tasks.json').write_text(json.dumps({'tasks': [{**task, 'gold_context_ids': ['a']}]}), encoding='utf-8')
        with self.assertRaises(ValueError):
            audit_qasper_result(workspace, {'metrics': {}, 'config': {'dataset': 'qasper', 'seeds': [13, 17]},
                                            'diagnostics': {'ranking_file': 'results/rankings.jsonl'}}, contract)

    def test_two_measurements_keep_rankings_when_workspace_path_is_reused(self):
        from evals.audit_auto_research_results import audit
        run = self.root / 'run'
        self.tool.output_root = run / 'coding-tools'
        workspace = self.tool.work_root / self.job['id']
        (workspace / 'data/qasper').mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('pass', encoding='utf-8')
        inputs = {'data/qasper/tasks.json': {'tasks': [
            {'id': 'q', 'source_id': 'p', 'gold_context_ids': ['a', 'b']}]},
            'data/qasper/corpus.json': [{'id': i, 'source_id': 'p'} for i in 'ab']}
        contract = {'name': 'qasper_fractional_recall_v1'}
        for key, name in [('tasks', 'data/qasper/tasks.json'), ('corpus', 'data/qasper/corpus.json')]:
            raw = json.dumps(inputs[name]).encode()
            (workspace / name).write_bytes(raw)
            contract[key + '_path'] = name
            contract[key + '_sha256'] = hashlib.sha256(raw).hexdigest()
        payload = json.loads(self.store.job(self.space, self.job['id'])['payload'])
        payload['auto_research']['metric_contract'] = contract
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json.dumps(payload), self.job['id']))
        tasks = []
        script_metrics = {'recall_at_5': 1}
        for version, ids in [(1, ['a']), (2, ['a', 'b'])]:
            task = self.tool.submit(self.job, f'run-{version}', {**self.request, 'execution_only': True,
                'seconds': 0, 'token_budget': 0, 'plan': json.dumps({'version': version})})
            def runner(*args, **kwargs):
                (workspace / 'rankings.jsonl').write_text(json.dumps({
                    'seed': 13, 'task_id': 'q', 'rankings': ids}) + '\n', encoding='utf-8')
                (workspace / 'result.json').write_text(json.dumps({'metrics': script_metrics,
                    'config': {'dataset': 'qasper', 'seeds': [13]},
                    'diagnostics': {'ranking_path': 'rankings.jsonl'}}), encoding='utf-8')
                return {'exit_code': 0, 'termination': 'completed', 'seconds': .1, 'stdout': '', 'stderr': ''}
            with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
                 patch('research_agent.coding_tool.run_process', side_effect=runner):
                self.tool.execute(self.space, task['id'])
            tasks.append(self.tool.get(self.space, task['id']))
            self.assertEqual(tasks[-1]['status'], 'completed', tasks[-1]['state'].get('error'))
        (run / 'inputs.json').write_text(json.dumps({'workspace': str(workspace), 'inputs': [
            {'destination': name, 'sha256': hashlib.sha256((workspace / name).read_bytes()).hexdigest()}
            for name in inputs]}), encoding='utf-8')
        (run / 'coding-tasks.json').write_text(json.dumps(tasks), encoding='utf-8')
        result = audit(run, self.root / 'audit', 'qasper')
        self.assertTrue(result['scores_reproduced'], result)
        self.assertEqual([m['metrics']['recall_at_5'] for m in result['measurements']], [.5, 1.0])
        artifact = tasks[0]['state']['measurements'][0]['prediction_artifacts']['ranking_path']
        archived = run / 'coding-tools' / tasks[0]['id'] / artifact['path']
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(), artifact['sha256'])
        archived.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            audit(run, self.root / 'audit-tampered', 'qasper')
        archived.unlink()
        with self.assertRaises(OSError):
            audit(run, self.root / 'audit-missing', 'qasper')
        # Malformed metric types retain both the raw output and execution receipt.
        script_metrics = 123
        invalid = self.tool.submit(self.job, 'invalid-metrics', {**self.request, 'execution_only': True,
            'seconds': 0, 'token_budget': 0, 'plan': json.dumps({'version': 3})})
        with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner):
            self.tool.execute(self.space, invalid['id'])
        failed = self.tool.get(self.space, invalid['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(failed['state']['measurements'][0]['valid'])
        output = self.tool.output_root / invalid['id']
        self.assertEqual(json.loads((output / 'result-00.json').read_text(encoding='utf-8'))['metrics'], 123)
        self.assertEqual(json.loads((output / 'measurement-00.json').read_text(encoding='utf-8'))['receipt']['exit_code'], 0)

    def test_scope_authorization_idempotence_and_budget_reservation(self):
        task = self.tool.submit(self.job, 'call-one', self.request)
        self.assertEqual(self.tool.submit(self.job, 'call-one', self.request)['id'], task['id'])
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'call-one', {**self.request, 'task': 'different'})
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'call-two', self.request)
        self.tool.save(task['id'], {}, 'failed')
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'call-two', {**self.request, 'token_budget': 6000})
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE research_jobs SET kind='RESEARCH' WHERE id=?", (self.job['id'],))
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'call-two', self.request)

    def test_artifact_contract_rejects_external_paths_and_nonfinite_results(self):
        for path in ('../secret.py', 'C:/secret.py', '/secret.py', 'a\\b.py', 'a/.env', 'a//b.py'):
            command = {**self.request['commands'][0], 'script': path}
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_request({**self.request, 'commands': [command]})
        self.run_tool(metrics={'score': float('nan')})
        result = self.tool.get(self.space, self.task['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['state']['measurements'][0]['valid'])

    def test_invalid_nested_metrics_are_archived_without_becoming_valid_measurements(self):
        self.run_tool(metrics={'score': .4, 'confusion_matrix': [[1, 2], [3, 4]]})
        task = self.tool.get(self.space, self.task['id'])
        self.assertEqual(task['status'], 'failed')
        measurement = task['state']['measurements'][0]
        self.assertFalse(measurement['valid'])
        self.assertEqual(measurement['metrics'], {})
        self.assertIn('diagnostics', measurement['error'])
        raw = (self.tool.output_root/self.task['id']/'result-00.json').read_bytes()
        self.assertEqual(json.loads(raw)['metrics']['confusion_matrix'], [[1, 2], [3, 4]])
        self.assertEqual(measurement['result_sha256'], hashlib.sha256(raw).hexdigest())

    def test_directory_protection_freezes_contents_before_budget_reservation(self):
        workspace = self.tool.work_root/self.job['id']
        source = workspace/'references'/'official'
        source.mkdir(parents=True)
        (source/'score.py').write_text('fixed score')
        task = self.tool.submit(self.job, 'directory', {**self.request, 'protected_files': ['references/official']})
        frozen = task['state']['protected']
        self.tool.check_protected(workspace, frozen)
        (source/'added.py').write_text('changed input tree')
        with self.assertRaises(Conflict):
            self.tool.check_protected(workspace, frozen)
        with self.assertRaises(FileNotFoundError):
            # A malformed path fails before reserving a task or resources.
            self.tool.check_protected(workspace, {'missing': 'hash'})

    def test_missing_protected_path_does_not_spend_an_attempt(self):
        with self.assertRaises(FileNotFoundError):
            self.tool.submit(self.job, 'bad-input', {**self.request, 'protected_files': ['missing']})
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['coding_calls'], 3)
        self.assertEqual(self.tool.submit(self.job, 'bad-input', self.request)['status'], 'queued')

    def test_confirmed_prelaunch_failure_releases_allowance_but_counts_attempt(self):
        task = self.tool.submit(self.job, 'prelaunch', {**self.request, 'seconds': 90, 'token_budget': 6000})
        with patch('research_agent.research_project.python_for', side_effect=ValueError('environment unavailable')), \
             patch('research_agent.coding_tool.run_process') as runner:
            self.tool.execute(self.space, task['id'])
            runner.assert_not_called()
        remaining = self.tool.remaining(self.store.job(self.space, self.job['id']))
        self.assertEqual(remaining['coding_seconds'], 90)
        self.assertEqual(remaining['coding_tokens'], 6000)
        self.assertEqual(remaining['coding_calls'], 2)
        self.tool.submit(self.job, 'corrected', {**self.request, 'seconds': 90, 'token_budget': 6000})

    def test_native_preflight_failure_releases_only_unstarted_execution(self):
        from contextlib import contextmanager
        from research_agent.experiment_process import NativeCommand, run_process
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('pass')
        request = {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0}
        task = self.tool.submit(self.job, 'native-preflight', request)
        @contextmanager
        def blocked(*args, **kwargs):
            raise RuntimeError('Unfinished native ACL cleanup requires review')
            yield
        with patch('research_agent.coding_tool.run_process', run_process), \
             patch('research_agent.coding_tool.sandbox_command', return_value=NativeCommand(['fixture'], workspace, [])), \
             patch('research_agent.experiment_acl.scoped_denials', blocked), \
             patch('research_agent.experiment_process._run_process') as native:
            self.tool.execute(self.space, task['id'])
            native.assert_not_called()
        failed = self.tool.get(self.space, task['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('Unfinished native ACL', failed['state']['error'])
        self.assertNotIn('measurement_started', failed['state'])
        self.assertTrue(failed['state']['execution_not_started'])
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['experiment_seconds'], 30)
        second = self.tool.submit(self.job, 'unknown-native', request)
        with patch('research_agent.coding_tool.sandbox_command', return_value=['fixture']), \
             patch('research_agent.coding_tool.run_process', side_effect=RuntimeError('unknown after launch')):
            self.tool.execute(self.space, second['id'])
        unknown = self.tool.get(self.space, second['id'])
        self.assertEqual(unknown['state']['measurement_started'], 0)
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['experiment_seconds'], 20)

    def test_unknown_launched_execution_keeps_its_budget_reservation(self):
        task = self.tool.submit(self.job, 'unknown', {**self.request, 'seconds': 90})
        self.tool.save(task['id'], {'coding_started': 'persisted-before-launch'}, 'interrupted')
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['coding_seconds'], 0)
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'cannot-replay', self.request)

    def test_frozen_files_cannot_be_removed_by_later_model_delegations(self):
        workspace = self.tool.work_root/self.job['id']
        workspace.mkdir(parents=True)
        (workspace/'scorer.py').write_bytes(b'fixed evaluator')
        first = self.tool.submit(self.job, 'first', {**self.request, 'protected_files': ['scorer.py']})
        self.tool.save(first['id'], {'protected': {'scorer.py': hashlib.sha256(b'fixed evaluator').hexdigest()}}, 'completed')
        second = self.tool.submit(self.job, 'second', self.request)
        self.assertIn('scorer.py', second['request']['protected_files'])
        self.assertEqual(self.tool.submit(self.job, 'second', self.request)['id'], second['id'])
        self.tool.save(second['id'], {}, 'failed')
        (workspace/'scorer.py').write_bytes(b'changed evaluator')
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'third', self.request)

    def test_repair_can_edit_generated_source_but_keeps_input_frozen(self):
        workspace = self.tool.work_root / self.job['id']
        (workspace / 'data').mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('broken')
        first = self.tool.submit(self.job, 'first-repair', {**self.request, 'protected_files': ['data']})
        self.tool.save(first['id'], {
            'before': {}, 'after': {'evaluate.py': 'broken'},
            'protected': first['state']['protected'],
        }, 'failed')

        repaired = self.tool.submit(self.job, 'repair', {
            **self.request,
            'task': 'Fix the runtime error in evaluate.py',
            'protected_files': ['data', 'evaluate.py'],
        })

        self.assertEqual(repaired['request']['protected_files'], ['data'])
        self.assertEqual(repaired['state']['repairable_generated_files'], ['evaluate.py'])

    def test_later_plan_revision_can_edit_generated_source_but_keeps_inputs_frozen(self):
        workspace = self.tool.work_root / self.job['id']
        (workspace / 'data').mkdir(parents=True)
        (workspace / 'data' / 'labels.jsonl').write_text('{"label":"fixed"}\n', encoding='utf8')
        (workspace / 'evaluate.py').write_text('print("fixed scorer")\n', encoding='utf8')
        (workspace / 'model.py').write_text('VERSION = 1\n', encoding='utf8')

        first = self.tool.submit(self.job, 'initial-plan', {
            **self.request, 'protected_files': ['data', 'evaluate.py']})
        first_state = first['state']
        first_state.update({
            'before': {'evaluate.py': 'print("fixed scorer")\n'},
            'after': {'evaluate.py': 'print("fixed scorer")\n', 'model.py': 'VERSION = 1\n'},
        })
        self.tool.save(first['id'], first_state, 'completed')

        revised = self.tool.submit(self.job, 'method-revision', {
            **self.request, 'task': 'Implement the revised method after measured feedback',
            'protected_files': ['data', 'evaluate.py']})
        self.assertEqual(revised['state']['repairable_generated_files'], ['model.py'])
        self.assertEqual(revised['request']['protected_files'], ['data', 'evaluate.py'])

        calls = []
        def runner(args, workspace, **kwargs):
            calls.append(args)
            if args == ['codex']:
                (workspace / 'model.py').write_text('VERSION = 2\n', encoding='utf8')
                stdout = json.dumps({'type': 'turn.completed',
                                     'usage': {'input_tokens': 100, 'output_tokens': 50}})
            else:
                (workspace / 'result.json').write_text('{"metrics":{"score":0.8}}', encoding='utf8')
                stdout = ''
            return {'exit_code': 0, 'termination': 'completed', 'seconds': 1,
                    'tool_calls': 1, 'stdout': stdout, 'stderr': ''}

        with patch.dict(os.environ, {'SUDOCODE_API_KEY': 'test-secret'}), \
             patch('research_agent.coding_tool.coding_command', return_value=['codex']), \
             patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.materialize_readonly_files',
                   wraps=materialize_readonly_files) as handoff, \
             patch('research_agent.coding_tool.run_process', side_effect=runner):
            self.tool.execute(self.space, revised['id'])

        result = self.tool.get(self.space, revised['id'])
        self.assertEqual(result['status'], 'completed', result['state'].get('error'))
        self.assertEqual(calls, [['codex'], ['sandbox']])
        self.assertEqual((workspace / 'model.py').read_text(encoding='utf8'), 'VERSION = 2\n')
        self.assertEqual((workspace / 'evaluate.py').read_text(encoding='utf8'), 'print("fixed scorer")\n')
        self.assertEqual((workspace / 'data' / 'labels.jsonl').read_text(encoding='utf8'), '{"label":"fixed"}\n')
        handed_off = [set(call.args[1]) for call in handoff.call_args_list]
        self.assertIn({'model.py'}, handed_off)
        self.assertTrue(all('data' not in names for names in handed_off if names == {'model.py'}))

    def test_initial_measured_source_can_be_renamed_before_a_third_task(self):
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('fixed scorer', encoding='utf8')
        (workspace / 'old_method.py').write_text('version one', encoding='utf8')
        snapshot = self.tool.source_snapshot(workspace)
        first = self.tool.submit(self.job, 'seeded', {**self.request, 'execution_only': True,
                                 'seconds': 0, 'token_budget': 0, 'protected_files': ['evaluate.py']})
        self.tool.save(first['id'], {**first['state'], 'before': snapshot, 'after': snapshot,
                       'readonly_source_sha256': {name: hashlib.sha256(code.encode()).hexdigest() for name, code in snapshot.items()}}, 'completed')
        second = self.tool.submit(self.job, 'rename', self.request)
        (workspace / 'old_method.py').rename(workspace / 'new_method.py')
        after = self.tool.source_snapshot(workspace)
        self.tool.save(second['id'], {**second['state'], 'before': snapshot, 'after': after,
                       'readonly_source_sha256': {name: hashlib.sha256(code.encode()).hexdigest() for name, code in after.items()}}, 'completed')
        third = self.tool.submit(self.job, 'continue', self.request)
        self.assertNotIn('old_method.py', third['state']['editable_measured_sources'])
        self.assertIn('new_method.py', third['state']['editable_measured_sources'])
        self.assertIn('evaluate.py', third['request']['protected_files'])

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('RESEARCH_RUN_NATIVE_TESTS') == '1',
                         'opt-in native Windows sandbox check')
    def test_native_two_round_revision_preserves_frozen_scorer_and_data(self):
        from research_agent.experiment_process import sandbox_command
        self.tool.work_root = Path(__file__).resolve().parents[1] / 'experiments' / f'native-test-{self.job["id"]}'
        self.addCleanup(lambda: shutil.rmtree(self.tool.work_root, ignore_errors=True))
        workspace = self.tool.work_root / self.job['id']
        python = Path(__file__).resolve().parents[1] / '.venv-v3/Scripts/python.exe'
        if not python.is_file():
            python = Path(sys.executable)
        (workspace / 'data').mkdir(parents=True)
        (workspace / 'data' / 'labels.jsonl').write_text('fixed labels', encoding='utf8')
        scorer = ('import json,sys\nfrom pathlib import Path\n'
                  'value=int(Path("model.py").read_text().split("=")[1])\n'
                  'Path(sys.argv[1]).write_text(json.dumps({"metrics":{"score":value}}))\n')
        (workspace / 'evaluate.py').write_text(scorer, encoding='utf8')
        # Seeded code is frozen only while measured; later coding may evolve it
        # while explicit scorer/data protections persist across the campaign.
        (workspace / 'model.py').write_text('VERSION = 0\n', encoding='utf8')
        initial = self.tool.submit(self.job, 'native-initial-measurement', {
            **self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0,
            'protected_files': ['data', 'evaluate.py'],
            'commands': [{**self.request['commands'][0], 'args': ['result-0.json'], 'result_path': 'result-0.json'}]})
        self.tool.execute(self.space, initial['id'])
        measured = self.tool.get(self.space, initial['id'])
        self.assertEqual(measured['status'], 'completed', measured['state'].get('error'))
        self.assertEqual(measured['state']['measurements'][0]['metrics']['score'], 0)
        for version in (1, 2):
            request = {**self.request, 'task': f'Implement method version {version}',
                       'protected_files': ['data', 'evaluate.py'],
                       'commands': [{**self.request['commands'][0], 'args': [f'result-{version}.json'],
                                     'result_path': f'result-{version}.json'}]}
            task = self.tool.submit(self.job, f'native-{version}', request)
            # Exercise the native coding/measurement boundary without a paid model.
            code = ('import json\nfrom pathlib import Path\n'
                    'for name in ("evaluate.py", "data/labels.jsonl"):\n'
                    ' try: Path(name).write_text("tampered")\n'
                    ' except PermissionError: pass\n'
                    ' else: raise AssertionError("protected input writable: " + name)\n'
                    f'Path("model.py").write_text("VERSION = {version}\\n")\n'
                    'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":0,"output_tokens":0}}))\n')
            def native_coder(root, private, tokens, *, readonly_paths):
                return sandbox_command(root, [str(python), '-I', '-c', code], private, readonly_paths)
            with patch.dict(os.environ, {'SUDOCODE_API_KEY': 'unused-native-test'}), \
                 patch('research_agent.coding_tool.coding_command', side_effect=native_coder):
                self.tool.execute(self.space, task['id'])
            result = self.tool.get(self.space, task['id'])
            self.assertEqual(result['status'], 'completed', result['state'].get('error'))
            self.assertEqual(result['state']['measurements'][0]['metrics']['score'], version)
            self.assertEqual((workspace / 'evaluate.py').read_text(encoding='utf8'), scorer)
            self.assertEqual((workspace / 'data' / 'labels.jsonl').read_text(encoding='utf8'), 'fixed labels')

    def run_tool(self, metrics=None, write_result=True, stdout_prefix="", extra_result=None):
        self.task = self.tool.submit(self.job, 'call-one', self.request)
        workspace = self.tool.work_root/self.job['id']
        workspace.mkdir(parents=True)
        (workspace/'result.json').write_text('{"metrics":{"score":999}}')
        commands = []
        def runner(args, workspace, **kwargs):
            commands.append(args)
            if args == ['codex']:
                (workspace/'evaluate.py').write_text('print("real evaluator")\n')
                stdout = stdout_prefix + json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 50}})
            else:
                self.assertFalse((workspace/'result.json').exists())
                if write_result:
                    (workspace/'result.json').write_text(json.dumps({'metrics': metrics or {'score': .7}, 'split': 'dev', **(extra_result or {})}))
                stdout = ''
            return {'exit_code': 0, 'termination': 'completed', 'seconds': 1, 'tool_calls': 1, 'stdout': stdout, 'stderr': ''}
        with patch.dict(os.environ, {'SUDOCODE_API_KEY': 'test-secret'}), \
             patch('research_agent.coding_tool.coding_command', return_value=['codex']), \
             patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner):
            self.tool.execute(self.space, self.task['id'])
            self.tool.execute(self.space, self.task['id'])
        self.assertEqual(commands, [['codex'], ['sandbox']])

    def test_nonfinite_diagnostics_cannot_break_report_archival(self):
        self.run_tool(extra_result={'diagnostics': {'latency': float('nan')}})
        result = self.tool.get(self.space, self.task['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['state']['measurements'][0]['valid'])
        self.assertTrue((self.tool.output_root/self.task['id']/'result-00.json').is_file())

    def test_integrity_failure_invalidates_only_current_measurement(self):
        workspace = self.tool.work_root/self.job['id']
        workspace.mkdir(parents=True)
        (workspace/'evaluate.py').write_text('pass')
        request = {**self.request, 'commands': [{**self.request['commands'][0], 'role': role, 'name': role}
                                              for role in ('baseline', 'candidate')]}
        task = self.tool.submit(self.job, 'integrity-check', {**request, 'execution_only': True, 'seconds': 0, 'token_budget': 0})
        calls = []
        def runner(*args, **kwargs):
            calls.append(args)
            (workspace/'result.json').write_text('{"metrics":{"score":0.99}}')
            if len(calls) == 2:
                (workspace/'injected_method.py').write_text('changed source')
            return {'termination': 'completed', 'exit_code': 0, 'seconds': .1, 'stdout': '', 'stderr': ''}
        with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner):
            self.tool.execute(self.space, task['id'])
        task = self.tool.get(self.space, task['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertEqual([m['valid'] for m in task['state']['measurements']], [True, False])
        receipt = json.loads((self.tool.output_root/task['id']/'measurement-01.json').read_text(encoding='utf-8'))
        self.assertFalse(receipt['valid'])
        self.assertIn('代码快照', receipt['error'])
        self.assertEqual(len(calls), 2)

    def test_execution_receipts_resume_without_replaying_coding_or_measurement(self):
        self.run_tool()
        result = self.tool.get(self.space, self.task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['state']['measurements'][0]['metrics']['score'], .7)
        self.assertTrue((self.tool.output_root/self.task['id']/'diff.patch').is_file())

    def test_long_jsonl_receipt_retains_terminal_usage_and_resumes_without_replay(self):
        prefix = json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution',
                             'aggregated_output': 'x' * 17000}}) + '\n'
        self.run_tool(stdout_prefix=prefix)
        result = self.tool.get(self.space, self.task['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['state']['coding']['tokens'], 150)
        receipt = json.loads((self.tool.output_root/self.task['id']/'codex.json').read_text(encoding='utf-8'))
        self.assertGreater(len(receipt['stdout']), 17000)
        self.assertEqual(json.loads(receipt['stdout'].splitlines()[-1])['type'], 'turn.completed')

    def test_timeout_retains_receipt_and_never_replays_truncated_event(self):
        task = self.tool.submit(self.job, 'timeout', self.request)
        receipt = {'exit_code': 0, 'termination': 'timeout', 'seconds': 30, 'tool_calls': 1,
                   'stdout': json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Partial implementation saved'}}) + '\n{"type":', 'stderr': 'write failed'}
        def interrupted_writer(*args, **kwargs):
            (self.tool.work_root/self.job['id']/'partial.py').write_text('print(\"unfinished\")')
            return receipt
        with patch.dict(os.environ, {'SUDOCODE_API_KEY': 'test-secret'}), \
             patch('research_agent.coding_tool.coding_command', return_value=['codex']), \
             patch('research_agent.coding_tool.run_process', side_effect=interrupted_writer) as runner:
            self.tool.execute(self.space, task['id'])
            self.tool.execute(self.space, task['id'])
        self.assertEqual(runner.call_count, 1)
        state = self.tool.get(self.space, task['id'])['state']
        self.assertEqual(state['coding']['termination'], 'timeout')
        self.assertEqual(len(state['coding']['event_errors']), 1)
        self.assertIn('timeout', state['error'])
        self.assertIn('partial.py', state['available_files'])
        self.assertEqual(state['summary'], 'Partial implementation saved')
        self.assertTrue((self.tool.output_root/task['id']/'coding-source.json').is_file())
        self.assertFalse(state.get('coded'))
        self.assertFalse(state.get('measurements'))

    def test_overrun_charges_actual_tokens_and_allows_separately_budgeted_measurement(self):
        task = self.tool.submit(self.job, 'overrun', self.request)
        workspace = self.tool.work_root/self.job['id']
        def code(*args, **kwargs):
            (workspace/'evaluate.py').write_text('print("prepared")')
            return {'exit_code': 0, 'termination': 'completed', 'seconds': 2,
                    'stdout': json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 2400, 'output_tokens': 100}})}
        with patch.dict(os.environ, {'SUDOCODE_API_KEY': 'test-secret'}), \
             patch('research_agent.coding_tool.coding_command', return_value=['codex']), \
             patch('research_agent.coding_tool.run_process', side_effect=code):
            self.tool.execute(self.space, task['id'])
        failed = self.tool.get(self.space, task['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('run_experiments', failed['state']['error'])
        self.assertIn('evaluate.py', failed['state']['available_files'])
        remaining = self.tool.remaining(self.store.job(self.space, self.job['id']))
        self.assertEqual(remaining['coding_tokens'], 3500)
        self.assertEqual(remaining['coding_seconds'], 88)
        self.assertEqual(remaining['experiment_seconds'], 30)
        with self.assertRaises(Conflict):
            self.tool.submit(self.job, 'overspend', {**self.request, 'token_budget': 4000})
        request = {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0}
        measurement = self.tool.submit(self.job, 'measure', request)
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['coding_calls'], 2)
        (workspace/'result.json').write_text('{"metrics":{"score":999}}')
        def measure(*args, **kwargs):
            self.assertFalse((workspace/'result.json').exists())
            (workspace/'result.json').write_text('{"metrics":{"score":0.5}}')
            return {'exit_code': 0, 'termination': 'completed', 'seconds': 1, 'stdout': ''}
        with patch('research_agent.coding_tool.coding_command') as cli, \
             patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']) as sandbox, \
             patch('research_agent.coding_tool.run_process', side_effect=measure) as runner:
            self.tool.execute(self.space, measurement['id'])
            self.tool.execute(self.space, measurement['id'])
            cli.assert_not_called()
            self.assertEqual(runner.call_count, 1)
            self.assertIn(workspace/'evaluate.py', sandbox.call_args.kwargs['readonly_paths'])
        result = self.tool.get(self.space, measurement['id'])
        self.assertEqual(result['status'], 'completed', result['state'].get('error'))
        self.assertEqual(result['state']['measurements'][0]['metrics']['score'], .5)
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['experiment_seconds'], 29)
        # A later method revision may edit code; per-measurement freezing is not permanent.
        self.assertNotIn('evaluate.py', result['request']['protected_files'])

    def test_execution_only_rejects_code_drift_before_launch(self):
        workspace = self.tool.work_root/self.job['id']
        workspace.mkdir(parents=True)
        (workspace/'evaluate.py').write_text('print("v1")')
        task = self.tool.submit(self.job, 'evaluate', {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0})
        (workspace/'evaluate.py').write_text('print("different")')
        with patch('research_agent.coding_tool.run_process') as runner:
            self.tool.execute(self.space, task['id'])
            runner.assert_not_called()
        self.assertEqual(self.tool.get(self.space, task['id'])['status'], 'failed')
        for fields in ({'token_budget': 2000}, {'commands': []}, {'execution_only': 'true'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_request({**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0, **fields})

    def test_stale_result_cannot_be_counted_when_command_does_not_write_it(self):
        self.run_tool(write_result=False)
        self.assertEqual(self.tool.get(self.space, self.task['id'])['status'], 'failed')

    def test_cancelled_parent_prevents_execution(self):
        task = self.tool.submit(self.job, 'call-one', self.request)
        self.store.cancel(self.space, self.job['id'])
        with patch('research_agent.coding_tool.run_process') as runner:
            self.tool.execute(self.space, task['id'])
            runner.assert_not_called()
        self.assertEqual(self.tool.get(self.space, task['id'])['status'], 'cancelled')

    def test_readonly_handoff_preserves_exact_bytes_and_rejects_links(self):
        workspace = self.root/'handoff'
        workspace.mkdir()
        source = workspace/'method.py'
        original = b'\xef\xbb\xbfprint("method")\r\n'
        source.write_bytes(original)
        materialize_readonly_files(workspace, ['method.py'])
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(list(workspace.iterdir()), [source])
        alias = workspace/'alias.py'
        os.link(source, alias)
        with self.assertRaises(ValueError):
            materialize_readonly_files(workspace, ['method.py'])
        self.assertEqual(source.read_bytes(), original)

    def test_frozen_files_and_parent_links_are_checked(self):
        root = self.root/'example'
        (root/'nested').mkdir(parents=True)
        (root/'nested'/'scorer.py').write_text('pass')
        self.assertEqual(regular_file(root, 'nested/scorer.py'), b'pass')
        with self.assertRaises(Conflict):
            self.tool.check_protected(root, {'nested/scorer.py': 'wrong-hash'})



    def test_host_model_contract_rejects_result_and_frozen_path_collisions(self):
        spec = {'input_path': 'requests.jsonl', 'output_path': 'result.json', 'model_id': 'default',
                'max_requests': 1, 'seconds': 30}
        with self.assertRaises(ValueError):
            validate_request({**self.request, 'model_requests': spec})
        with self.assertRaises(ValueError):
            validate_request({**self.request, 'model_requests': {**spec, 'output_path': 'responses.jsonl'},
                              'protected_files': ['responses.jsonl']})

    def test_sequential_model_calls_use_previous_answers_and_persist_receipts(self):
        import time
        seen = []
        class Model:
            last_usage = {'total_tokens': 9}
            def complete(inner, messages, tools):
                seen.append(messages[-1]['content'])
                self.assertEqual(tools, [])
                return ModelDecision('final', content='memory fact' if len(seen) == 1 else 'updated fact')
        self.tool.app.model_factory = lambda: Model()
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('pass', encoding='utf-8')
        task = self.tool.submit(self.job, 'sequential', {**self.request, 'execution_only': True,
            'seconds': 0, 'token_budget': 0, 'model_requests': {
                'mode': 'stdio', 'model_id': 'default', 'max_requests': 2, 'seconds': 30}})
        def runner(*args, model_request, **kwargs):
            first = model_request({'id': 'construct', 'messages': [{'role': 'user', 'content': 'turn'}]}, time.monotonic()+10)
            second = model_request({'id': 'evolve', 'messages': [{'role': 'user', 'content': first['content']}]}, time.monotonic()+10)
            self.assertEqual(second['content'], 'updated fact')
            (workspace/'result.json').write_text(json.dumps({'metrics': {'score': 1}}), encoding='utf-8')
            return {'termination': 'completed', 'exit_code': 0, 'seconds': .1, 'stdout': '', 'stderr': ''}
        with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner):
            self.tool.execute(self.space, task['id'])
        result = self.tool.get(self.space, task['id'])
        self.assertEqual(result['status'], 'completed', result['state'].get('error'))
        self.assertEqual(seen, ['turn', 'memory fact'])
        self.assertEqual(result['state']['measurements'][0]['host_model_request_range'], [0, 2])
        self.assertNotIn('content', result['state']['model_requests'][0])
        receipts = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((self.tool.output_root/task['id']).glob('model-request-*.json'))]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[1]['request']['messages'][0]['content'], 'memory fact')
        self.assertEqual(receipts[1]['content'], 'updated fact')

    def test_queued_model_budget_is_reserved_and_stdio_fields_are_strict(self):
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace/'evaluate.py').write_text('pass', encoding='utf-8')
        spec = {'mode': 'stdio', 'model_id': 'default', 'max_requests': 16, 'seconds': 30}
        request = {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0, 'model_requests': spec}
        self.tool.submit(self.job, 'reserve-models', request)
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['experiment_model_calls'], 0)
        with self.assertRaisesRegex(Conflict, '未完成'):
            self.tool.submit(self.job, 'overbook', {**request, 'model_requests': {**spec, 'max_requests': 1}})
        for change in ({'input_path': 'requests.jsonl'}, {'mode': 'unknown'}, {'max_requests': True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_request({**request, 'model_requests': {**spec, **change}})

    def test_checkpoint_delay_cannot_extend_experiment_model_deadline(self):
        from unittest.mock import Mock
        model = Mock()
        model.request_deadline = None
        model.usage_records = []
        model.last_usage = {}
        model.request_guard = None
        model.stream_callback = None
        model.complete.return_value = ModelDecision('final', content='late answer')
        self.tool.app.model_factory = lambda: model
        output = self.root/'expired-model'
        output.mkdir()
        clock = [10.0]
        def checkpoint(): clock[0] = 11.0
        with patch('research_agent.coding_tool.time.monotonic', side_effect=lambda: clock[0]), \
                self.assertRaises(RuntimeError):
            self.tool._call_host_model(output, {}, {'mode':'stdio','model_id':'default','max_requests':1,'seconds':30},
                {'id':'r1','messages':[{'role':'user','content':'test'}]}, checkpoint, lambda:False, deadline=10.5)
        model.complete.assert_not_called()

    def test_sequential_host_denies_duplicate_budget_and_unknown_replay(self):
        output = self.root/'sequential-receipts'
        output.mkdir()
        calls = []
        class Model:
            def complete(inner, messages, tools):
                calls.append(1)
                return ModelDecision('final', content='answer')
        self.tool.app.model_factory = lambda: Model()
        spec = {'mode': 'stdio', 'model_id': 'default', 'max_requests': 2, 'seconds': 30}
        item = {'id': 'r1', 'messages': [{'role': 'user', 'content': 'turn'}]}
        state = {}
        self.tool._call_host_model(output, state, spec, item, lambda: None, lambda: False)
        with self.assertRaisesRegex(Conflict, '重复'):
            self.tool._call_host_model(output, state, spec, item, lambda: None, lambda: False)
        state['model_request_started'] = 1
        with self.assertRaisesRegex(Conflict, '收据缺失'):
            self.tool._call_host_model(output, state, spec, {**item, 'id': 'r2'}, lambda: None, lambda: False)
        state.pop('model_request_started')
        with self.assertRaisesRegex(Conflict, '预算'):
            self.tool._call_host_model(output, state, {**spec, 'max_requests': 1}, {**item, 'id': 'r2'}, lambda: None, lambda: False)
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('RESEARCH_RUN_NATIVE_TESTS') == '1',
                         'opt-in native Windows sandbox check')
    def test_native_sequential_model_exchange_keeps_credentials_on_host(self):
        self.tool.work_root = Path(__file__).resolve().parents[1]/'experiments'/f'native-model-{self.job["id"]}'
        self.addCleanup(lambda: shutil.rmtree(self.tool.work_root, ignore_errors=True))
        workspace = self.tool.work_root/self.job['id']
        workspace.mkdir(parents=True)
        (workspace/'evaluate.py').write_text(
            'import json,sys,os\nfrom pathlib import Path\n'
            'assert "RESEARCH_TEST_API_KEY" not in os.environ\n'
            'for i in range(2):\n'
            ' text="initial turn" if i==0 else response["content"]\n'
            ' print("RESEARCH_MODEL_REQUEST "+json.dumps({"id":str(i),"messages":[{"role":"user","content":text}]}),flush=True)\n'
            ' response=json.loads(sys.stdin.readline())\n'
            ' assert response["id"]==str(i)\n'
            'assert response["content"]=="dependent answer"\n'
            'Path("result.json").write_text(json.dumps({"metrics":{"calls":2}}))\n', encoding='utf-8')
        seen = []
        class Model:
            def complete(inner, messages, tools):
                seen.append(messages[-1]['content'])
                return ModelDecision('final', content='first fact' if len(seen)==1 else 'dependent answer')
        self.tool.app.model_factory = lambda: Model()
        task = self.tool.submit(self.job, 'native-sequential', {**self.request, 'execution_only': True,
            'seconds': 0, 'token_budget': 0, 'model_requests': {
                'mode': 'stdio', 'model_id': 'default', 'max_requests': 2, 'seconds': 30}})
        with patch.dict(os.environ, {'RESEARCH_TEST_API_KEY': 'dummy-not-a-credential'}):
            self.tool.execute(self.space, task['id'])
        result = self.tool.get(self.space, task['id'])
        self.assertEqual(result['status'], 'completed', result['state'].get('error'))
        self.assertEqual(seen, ['initial turn', 'first fact'])
        self.assertEqual(result['state']['measurements'][0]['metrics']['calls'], 2)


    def test_host_model_batch_runs_on_host_and_writes_only_responses(self):
        payload = json.loads(self.store.job(self.space, self.job['id'])['payload'])
        payload['auto_research']['budget'].update(experiment_model_calls=2, experiment_model_seconds=30)
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json.dumps(payload), self.job['id']))
        class FakeModel:
            name = 'host-test-model'
            last_usage = {'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7}
            calls = []
            def complete(self, messages, tools):
                self.calls.append((messages, tools))
                return ModelDecision('final', content='host response ' + str(len(self.calls)))
        model = FakeModel()
        self.tool.app.model_factory = lambda: model
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'requests.jsonl').write_text(
            json.dumps({'id': 'r1', 'messages': [{'role': 'user', 'content': 'first'}]}, ensure_ascii=False) + '\n' +
            json.dumps({'id': 'r2', 'messages': [{'role': 'user', 'content': 'second'}]}, ensure_ascii=False) + '\n',
            encoding='utf-8')
        output = self.tool.output_root / 'model-batch'
        output.mkdir(parents=True)
        state = {}
        checkpoints = []
        def checkpoint(): checkpoints.append(json.loads(json.dumps(state)))
        def write(name, value): (output / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        spec = {'input_path': 'requests.jsonl', 'output_path': 'responses.jsonl', 'model_id': 'default',
                'max_requests': 2, 'seconds': 30}
        self.tool._run_host_model(workspace, output, state, spec, checkpoint, lambda: False, write)
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(all(call[1] == [] for call in model.calls))
        rows = [json.loads(line) for line in (workspace / 'responses.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['content'] for row in rows], ['host response 1', 'host response 2'])
        self.assertEqual(state['model_summary']['requests'], 2)
        self.assertEqual(len(list(output.glob('model-request-*.json'))), 2)
        self.assertGreaterEqual(len(checkpoints), 3)
        self.assertEqual(self.tool.remaining(self.store.job(self.space, self.job['id']))['experiment_model_calls'], 2)

    def test_model_payload_survives_receipts_and_batch_checkpoint_resume(self):
        from research_agent.trace import redact
        answer = '{"key":"compiler","value":17}'
        class Model:
            calls = 0
            def complete(inner, messages, tools):
                inner.calls += 1
                return ModelDecision('final', content=answer)
        model = Model()
        self.tool.app.model_factory = lambda: model
        workspace = self.tool.work_root/self.job['id']
        workspace.mkdir(parents=True)
        request = {'id': 'r1', 'messages': [{'role': 'user', 'content': 'Return a key/value memory.'}]}
        (workspace/'requests.jsonl').write_text(json.dumps(request)+'\n', encoding='utf-8')
        output = self.tool.output_root/'exact-payload'
        output.mkdir(parents=True)
        state = {}
        spec = {'input_path': 'requests.jsonl', 'output_path': 'responses.jsonl', 'model_id': 'default',
                'max_requests': 1, 'seconds': 30}
        response = self.tool._call_host_model(output, state, spec, request, lambda: None, lambda: False)
        self.assertEqual(response['content'], answer)
        self.tool._run_host_model(workspace, output, redact(state, limit=None), spec,
                                  lambda: None, lambda: False, lambda *a: None)
        self.assertEqual(json.loads((workspace/'responses.jsonl').read_text())['content'], answer)
        self.assertEqual(model.calls, 1)

    def test_host_model_missing_receipt_is_never_replayed(self):
        payload = json.loads(self.store.job(self.space, self.job['id'])['payload'])
        payload['auto_research']['budget'].update(experiment_model_calls=1, experiment_model_seconds=30)
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json.dumps(payload), self.job['id']))
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'requests.jsonl').write_text(
            json.dumps({'id': 'r1', 'messages': [{'role': 'user', 'content': 'first'}]}) + '\n', encoding='utf-8')
        output = self.tool.output_root / 'missing-receipt'
        output.mkdir(parents=True)
        state = {'model_request_started': 0}
        calls = []
        class FakeModel:
            last_usage = {}
            def complete(self, messages, tools):
                calls.append(1)
                return ModelDecision('final', content='should not run')
        self.tool.app.model_factory = lambda: FakeModel()
        with self.assertRaises(Conflict):
            self.tool._run_host_model(workspace, output, state, {
                'input_path': 'requests.jsonl', 'output_path': 'responses.jsonl', 'model_id': 'default',
                'max_requests': 1, 'seconds': 30}, lambda: None, lambda: False, lambda n, v: None)
        self.assertEqual(calls, [])


    def test_host_model_response_is_available_to_host_measurement_without_key(self):
        payload = json.loads(self.store.job(self.space, self.job['id'])['payload'])
        payload['auto_research']['budget'].update(experiment_model_calls=1, experiment_model_seconds=30)
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET payload=? WHERE id=?', (json.dumps(payload), self.job['id']))
        class FakeModel:
            last_usage = {'total_tokens': 5}
            def complete(self, messages, tools):
                return ModelDecision('final', content='model answer')
        self.tool.app.model_factory = lambda: FakeModel()
        workspace = self.tool.work_root / self.job['id']
        workspace.mkdir(parents=True)
        (workspace / 'evaluate.py').write_text('pass', encoding='utf-8')
        (workspace / 'requests.jsonl').write_text(
            json.dumps({'id': 'r1', 'messages': [{'role': 'user', 'content': 'evaluate'}]}) + '\n', encoding='utf-8')
        request = {**self.request, 'execution_only': True, 'seconds': 0, 'token_budget': 0,
                   'model_requests': {'input_path': 'requests.jsonl', 'output_path': 'responses.jsonl',
                                      'model_id': 'default', 'max_requests': 1, 'seconds': 30}}
        task = self.tool.submit(self.job, 'host-model-integration', request)
        def runner(*args, **kwargs):
            response = json.loads((workspace / 'responses.jsonl').read_text(encoding='utf-8').strip())
            self.assertEqual(response['content'], 'model answer')
            (workspace / 'result.json').write_text(json.dumps({'metrics': {'score': .8}, 'config': {
                'dataset': 'fixture', 'dataset_version': 'v1', 'split': 'test', 'seeds': [1]}}), encoding='utf-8')
            return {'exit_code': 0, 'termination': 'completed', 'seconds': .1, 'stdout': '', 'stderr': ''}
        with patch('research_agent.coding_tool.sandbox_command', return_value=['sandbox']), \
             patch('research_agent.coding_tool.run_process', side_effect=runner) as process:
            self.tool.execute(self.space, task['id'])
        result = self.tool.get(self.space, task['id'])
        self.assertEqual(result['status'], 'completed', result['state'].get('error'))
        self.assertEqual(result['state']['model_summary']['requests'], 1)
        self.assertTrue(result['state']['measurements'][0]['valid'])
        process.assert_called_once()


if __name__ == '__main__':
    unittest.main()


class HostModelLimitFeedbackTests(unittest.TestCase):
    def test_full_prompt_is_preserved_within_the_existing_request_total(self):
        from research_agent.coding_tool import CodingTool
        prompt = 'Original memory context: ' + 'memory fact. ' * 4000 + 'Question: names?'
        request = {'id': 'complete-context', 'messages': [
            {'role': 'system', 'content': 'Official system prompt.'},
            {'role': 'user', 'content': prompt}]}
        result = CodingTool._model_request(request)
        self.assertEqual(result, request)
        self.assertEqual(result['messages'][1]['content'], prompt)
        # Same total that the old 32-message x 32000-char contract already permitted.
        old_boundary = {'id': 'boundary', 'messages': [{'role': 'user', 'content': '汉' * 32000} for _ in range(32)]}
        self.assertEqual(CodingTool._model_request(old_boundary), old_boundary)
        joined = {'id': 'one-message', 'messages': [{'role': 'user', 'content': '汉' * (32 * 32000)}]}
        self.assertEqual(CodingTool._model_request(joined), joined)

    def test_request_total_and_feedback_do_not_depend_on_message_splitting(self):
        from research_agent.coding_tool import CodingTool, EXPERIMENT_RESULT_CONTRACT
        self.assertIn('1024000', EXPERIMENT_RESULT_CONTRACT)
        self.assertIn('字符', EXPERIMENT_RESULT_CONTRACT)
        rejected = {'id': 'large', 'messages': [{'role': 'system', 'content': '汉' * 512000},
                    {'role': 'user', 'content': 'private-prompt-' + '汉' * 512000}]}
        with self.assertRaises(ValueError) as caught:
            CodingTool._model_request(rejected)
        message = str(caught.exception)
        self.assertIn('1024015', message)
        self.assertIn('1024000', message)
        self.assertIn('字符', message)
        self.assertNotIn('private-prompt', message)
        with self.assertRaisesRegex(ValueError, '字符串'):
            CodingTool._model_request({'id': 'bad', 'messages': [{'role': 'user', 'content': None}]})
