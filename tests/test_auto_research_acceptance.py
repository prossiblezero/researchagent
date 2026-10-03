"""Acceptance must distinguish a tool failure from actual experiment feedback."""
import json
import unittest
from pathlib import Path

from evals.run_auto_research_acceptance import (feedback_received, independent_result_audit,
                                                method_iteration_evidence, multi_seed_evidence,
                                                stage_initial_source)


class AcceptanceFeedbackTests(unittest.TestCase):
    def test_reading_recovery_preserves_latest_failed_revision_and_checks_its_question(self):
        import hashlib
        import tempfile
        from evals import run_auto_research_acceptance as acceptance

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            question = 'Read the original benchmark protocol'
            case = {'reading': {'question': question, 'source_job': 'child-1'}}
            state = {'question': question + '\nExecution instructions', 'next_iteration': 16,
                     'pending_decision': None, 'revision_pending': True,
                     'draft_answer': 'Revised answer', 'answer_repair_rounds': 2}
            values = {'state-after.json': state, 'cases.json': case,
                      'run.json': {'status': 'insufficient', 'termination': 'model_error'},
                      'metrics.json': {'passed': False},
                      'recovery-source.json': {'child_id': 'child-1', 'original_config': {'max_rounds': 20}}}
            for name, value in values.items():
                (source/name).write_text(json.dumps(value), encoding='utf-8')
            before = (source/'state-after.json').read_bytes()
            recovered, config, origin = acceptance.reading_checkpoint(source, case)
            self.assertEqual(recovered, state)
            self.assertEqual(config['max_rounds'], 20)
            self.assertEqual(origin['child_id'], 'child-1')
            self.assertEqual(origin['checkpoint_sha256'], hashlib.sha256(before).hexdigest())
            self.assertEqual(origin['verification_question'], question)
            self.assertEqual((source/'state-after.json').read_bytes(), before)
            self.assertFalse((source/'state.sqlite').exists())
            for filename, replacement in (
                ('metrics.json', {'passed': True}),
                ('cases.json', {'reading': {'question': 'An unrelated question'}}),
                ('recovery-source.json', {'child_id': 'other-child', 'original_config': {}}),
                ('state-after.json', {**state, 'revision_pending': False}),
            ):
                with self.subTest(filename=filename):
                    (source/filename).write_text(json.dumps(replacement), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        acceptance.reading_checkpoint(source, case)
                    (source/filename).write_text(json.dumps(values[filename]), encoding='utf-8')

    def test_independent_result_audit_is_a_separate_gate_and_preserves_reported_values(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        audited = {
            'scores_reproduced': False,
            'measurements': [{
                'task_id': 'task-123456789', 'name': 'candidate', 'role': 'candidate',
                'reported_metrics': {'recall_at_5': .8, 'mrr_at_5': .5},
                'metrics': {'recall_at_5': .4, 'mrr_at_5': .5},
            }],
            'scope': 'independent test'
        }
        def fake_audit(_run, output, _dataset):
            output.mkdir()
            return audited

        with TemporaryDirectory() as folder, patch('evals.audit_auto_research_results.audit', side_effect=fake_audit):
            result = independent_result_audit(Path(folder), 'qasper', required=True)
            self.assertTrue(result['required'])
            self.assertTrue(result['completed'])
            self.assertFalse(result['scores_reproduced'])
            report = (Path(folder) / 'independent-audit' / 'report.md').read_text(encoding='utf-8')
            self.assertIn('0.800000', report)
            self.assertIn('0.400000', report)

    def test_multi_seed_gate_requires_all_roles_to_declare_the_same_seeds(self):
        def measurement(role, seeds):
            return {'name': role, 'role': role, 'result': {'config': {'seeds': seeds}}}
        good = [measurement(role, [13, 17, 29]) for role in ('baseline', 'candidate', 'ablation')]
        self.assertTrue(multi_seed_evidence(good, [13, 17, 29])['passed'])
        self.assertFalse(multi_seed_evidence(good[:2] + [measurement('ablation', [13])], [13, 17, 29])['passed'])
        self.assertFalse(multi_seed_evidence(good, [13])['passed'])

    def test_failed_tool_result_is_not_experiment_feedback(self):
        def received(value):
            return feedback_received([[{'role': 'tool', 'content': json.dumps({'UNTRUSTED_TOOL_DATA': value})}]], [{'id': 'coding-1'}])
        failed = received({'id': 'coding-1', 'status': 'failed', 'measurements': []})
        self.assertTrue(failed['model_received_coding_feedback'])
        self.assertFalse(failed['model_received_experiment_results'])
        result = received({'id': 'coding-1', 'measurements': [{'valid': True, 'metrics': {'macro_f1': .5}}]})
        self.assertTrue(result['model_received_experiment_results'])
        unrelated = received({'id': 'outside', 'measurements': [{'valid': True, 'metrics': {'macro_f1': .5}}]})
        self.assertFalse(unrelated['model_received_experiment_results'])


    def test_method_iteration_requires_measured_feedback_and_comparable_rerun(self):
        from copy import deepcopy
        plans = [{'version': 1, 'call_id': 'plan-1', 'method': 'unweighted classifier'},
                 {'version': 2, 'call_id': 'plan-2', 'method': 'balance training classes after weak minority F1'}]
        def task(task_id, plan):
            return {'id': task_id, 'request': {'plan': json.dumps(plan)}, 'state': {
                'after': {'evaluate.py': 'fixed scorer', 'method.py': plan['method']},
                'measurements': [{'name': role, 'role': role, 'valid': True, 'metrics': {'macro_f1': .4},
                    'script_sha256': 'fixed-evaluator', 'result': {'config': {
                        'dataset': 'SciFact', 'dataset_version': 'original', 'split': 'dev', 'seeds': [1]}}}
                    for role in ('baseline', 'candidate', 'ablation')]}}
        tasks = [task('first', plans[0]), task('rerun', plans[1])]
        request = {'purpose': 'auto_research', 'request_hash': 'plan-request',
            'response': {'kind': 'tool_call', 'tool_name': 'save_plan', 'call_id': 'plan-2'},
            'messages': [{'role': 'tool', 'content': json.dumps({'UNTRUSTED_TOOL_DATA': {
                'id': 'first', 'measurements': tasks[0]['state']['measurements']}})}]}
        result = method_iteration_evidence([request], tasks, plans)
        self.assertTrue(result['structural_path_completed'])
        self.assertEqual(result['iterations'][0]['source_changes'], ['method.py'])
        self.assertTrue(result['iterations'][0]['manual_method_and_scoring_review_required'])
        changed_method = deepcopy(tasks)
        for task_value in changed_method:
            task_value['state']['after'].pop('evaluate.py')
        for measurement in changed_method[1]['state']['measurements']:
            measurement['script_sha256'] = 'changed-method-script'
        self.assertTrue(method_iteration_evidence([request], changed_method, plans)['structural_path_completed'])
        # Providers may return inspect first and save_plan in the same response queue.
        queued = deepcopy(request)
        queued['response'] = {'kind': 'tool_call', 'tool_name': 'inspect', 'call_id': 'inspect-1',
                              'queued_tool_calls': [deepcopy(request['response'])]}
        self.assertTrue(method_iteration_evidence([queued], tasks, plans)['structural_path_completed'])
        queued['response']['queued_tool_calls'][0]['call_id'] = 'unsaved-plan'
        self.assertFalse(method_iteration_evidence([queued], tasks, plans)['structural_path_completed'])
        duplicate = deepcopy(request)
        duplicate['response']['queued_tool_calls'] = [deepcopy(request['response'])]
        self.assertEqual(len(method_iteration_evidence([duplicate], tasks, plans)['iterations']), 1)
        split = [deepcopy(tasks[0])]
        for measurement in tasks[1]['state']['measurements']:
            item = deepcopy(tasks[1])
            item['id'] = 'split-' + measurement['role']
            item['state']['measurements'] = [deepcopy(measurement)]
            split.append(item)
        separated = method_iteration_evidence([request], split, plans)
        self.assertTrue(separated['structural_path_completed'])
        self.assertEqual(set(separated['iterations'][0]['comparison_tasks']),
                         {'split-baseline', 'split-candidate', 'split-ablation'})
        split[-1]['state']['after']['method.py'] = 'different implementation'
        self.assertFalse(method_iteration_evidence([request], split, plans)['structural_path_completed'])
        # More tasks/plans alone, invented feedback, or a changed evaluator cannot pass.
        for failure in ('missing_feedback', 'invented_metrics', 'no_method_change', 'no_source_change', 'docs_only', 'no_rerun', 'changed_evaluator', 'different_data'):
            with self.subTest(failure=failure):
                req, ts, ps = deepcopy(request), deepcopy(tasks), deepcopy(plans)
                if failure == 'missing_feedback': req['messages'] = []
                if failure == 'invented_metrics':
                    data = json.loads(req['messages'][0]['content'])
                    data['UNTRUSTED_TOOL_DATA']['measurements'][1]['metrics'] = {'macro_f1': .9}
                    req['messages'][0]['content'] = json.dumps(data)
                if failure == 'no_method_change': ps[1]['method'] = ps[0]['method']
                if failure in ('no_source_change', 'docs_only'):
                    ts[1]['state']['after'] = deepcopy(ts[0]['state']['after'])
                    if failure == 'docs_only': ts[1]['state']['after']['plan.md'] = 'new proposal'
                if failure == 'no_rerun': ts[1]['state']['measurements'] = []
                if failure == 'changed_evaluator':
                    for m in ts[1]['state']['measurements']: m['script_sha256'] = 'changed-scoring'
                if failure == 'different_data':
                    for m in ts[1]['state']['measurements']: m['result']['config']['dataset_version'] = 'other'
                self.assertFalse(method_iteration_evidence([req], ts, ps)['structural_path_completed'])

    def test_initial_source_preserves_bytes_and_rejects_overwrites_or_private_paths(self):
        import hashlib
        import tempfile
        from pathlib import Path
        from research_agent.workbench_store import Conflict
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifact = root/'source.json'
            workspace = root/'workspace'
            source = 'print("prior generated code")\n'
            artifact.write_text(json.dumps({'method.py': source}), encoding='utf-8')
            result = stage_initial_source(workspace, artifact)
            self.assertEqual((workspace/'method.py').read_bytes(), source.encode())
            self.assertEqual(result['source_sha256']['method.py'], hashlib.sha256(source.encode()).hexdigest())
            self.assertEqual(stage_initial_source(workspace, artifact), result)
            artifact.write_text(json.dumps({'method.py': 'different'}))
            with self.assertRaises(Conflict): stage_initial_source(workspace, artifact)
            for name in ('../external.py', '.env', '.venv/setup.py', '.VENV/setup.py', 'data/claims_dev.jsonl'):
                artifact.write_text(json.dumps({name: 'disallowed'}))
                with self.subTest(name=name), self.assertRaises(ValueError): stage_initial_source(workspace, artifact)
            self.assertEqual((workspace/'method.py').read_bytes(), source.encode())


class ReadingEnvironmentTests(unittest.TestCase):
    def fixture(self, folder):
        from research_agent.library import Library
        from research_agent.retrieval import Retriever
        from research_agent.workbench_store import WorkbenchStore
        source = folder/'source'; source.mkdir()
        store = WorkbenchStore(source/'state.sqlite')
        space = store.save_space({'name': 'Originals', 'download_root': str(folder)})['id']
        chat = store.create_conversation(space, 'Independent session')['id']
        question = '仅当前会话，read the original memory construction method'
        job = store.enqueue(space, chat, question, '', [], kind='LOCAL_QA')
        store.claim_next()
        store.finish(job['id'], 'failed', 'Saved draft did not pass verification')
        library = Library(store)
        library.save(space, {'kind': 'paper', 'title': 'Memory paper', 'url': '',
            'canonical_id': 'original-memory', 'metadata': {}, 'warnings': [], 'boundary': '',
            'chunks': [{'page': 3, 'section': 'Method', 'line_start': 1, 'line_end': 1,
                        'text': 'Memory construction preserves the original content and timestamp for retrieval.'}]})
        hit = Retriever(store, library, dense=False).retrieve(space, 'memory construction', corpus='documents')['results'][0]
        return source, {'path': str(source), 'child_id': job['id'], 'verification_question': question}, hit['ref_id']

    def test_recovery_reads_original_and_denies_web_without_changing_source(self):
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from research_agent.contracts import ModelDecision
        from evals import run_auto_research_acceptance as acceptance
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            source, origin, ref = self.fixture(folder)
            before = (source/'state.sqlite').read_bytes()
            out = folder/'recovery'; out.mkdir()
            decisions = [ModelDecision(kind='tool_call', tool_name='read_evidence', call_id='read-local',
                         arguments={'ref_id': ref, 'adjacent': 0}),
                         ModelDecision(kind='tool_call', tool_name='search', query='memory paper', call_id='web'),
                         ModelDecision(kind='final', content='The original preserves content and timestamps.')]
            model = SimpleNamespace(name='fixture', supports_answer_verification=False,
                                    complete=Mock(side_effect=decisions))
            with patch('main.build_search', side_effect=AssertionError('Local recovery must not initialize web search')):
                agent = acceptance.reading_recovery_agent(out, None, {}, origin, model)
            result = agent.run(origin['verification_question'])
            events = [json.loads(v) for v in Path(result.trace_path).read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(e.get('reason') == 'external_sources_forbidden' for e in events))
            self.assertIn('preserves the original content', json.dumps(model.complete.call_args_list[1].args[0]))
            self.assertFalse(agent.allow_workspace_recall)
            self.assertFalse(agent.allow_cross_session)
            self.assertIsNotNone(agent.turn_seq)
            self.assertEqual((source/'state.sqlite').read_bytes(), before)
            binding = json.loads((out/'recovery-environment.json').read_text())
            self.assertEqual(binding['source_database'], str(source/'state.sqlite'))
            self.assertEqual(agent.retrieval.store.path, out/'state.sqlite')

    def test_chain_binding_rejects_missing_mismatched_and_existing_destinations(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from evals import run_auto_research_acceptance as acceptance
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            source, origin, ref = self.fixture(folder)
            chain = folder/'chain'; chain.mkdir()
            (chain/'recovery-source.json').write_text(json.dumps(origin))
            out = folder/'out'; out.mkdir()
            agent = acceptance.reading_recovery_agent(out, None, {}, {**origin, 'path': str(chain)}, Mock())
            self.assertEqual(agent.retrieval.read(agent.space_id, ref, adjacent=0)['page'], 3)
            for index, changes in enumerate(({'child_id': 'missing'}, {'verification_question': 'another question'},
                                           {'path': str(folder/'missing')})):
                dest = folder/str(index); dest.mkdir()
                with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                    acceptance.reading_recovery_agent(dest, None, {}, {**origin, **changes}, Mock())
                self.assertFalse((dest/'state.sqlite').exists())
            with self.assertRaises(FileExistsError):
                acceptance.reading_recovery_agent(out, None, {}, origin, Mock())
            (chain/'recovery-source.json').write_text(json.dumps({**origin, 'path': str(chain)}))
            with self.assertRaises(ValueError):
                acceptance.reading_recovery_agent(out, None, {}, {**origin, 'path': str(chain)}, Mock())

    def test_brainstorm_requires_its_own_recovery_contract_before_any_model_call(self):
        import sqlite3
        from contextlib import closing
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock
        from evals import run_auto_research_acceptance as acceptance
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            source, origin, _ = self.fixture(folder)
            question = 'Propose grounded innovations'
            with closing(sqlite3.connect(source/'state.sqlite')) as db, db:
                db.execute("UPDATE research_jobs SET kind='BRAINSTORM', question=? WHERE id=?",
                           (question, origin['child_id']))
            origin['verification_question'] = question
            before = (source/'state.sqlite').read_bytes()
            out = folder/'out'; out.mkdir()
            model = Mock()
            with self.assertRaises(ValueError):
                acceptance.reading_recovery_agent(out, None, {}, origin, model)
            model.complete.assert_not_called()
            self.assertFalse((out/'state.sqlite').exists())
            self.assertEqual((source/'state.sqlite').read_bytes(), before)

    def test_web_recovery_keeps_arxiv_source_filter(self):
        import sqlite3
        from contextlib import closing
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock, patch
        from research_agent.contracts import SearchResponse
        from evals import run_auto_research_acceptance as acceptance
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            source, origin, _ = self.fixture(folder)
            with closing(sqlite3.connect(source/'state.sqlite')) as db, db:
                db.execute("UPDATE research_jobs SET kind='RESEARCH', search_scope='arxiv' WHERE id=?", (origin['child_id'],))
            provider = Mock()
            provider.search.return_value = SearchResponse(True, 'query', [
                {'url': 'https://arxiv.org/abs/2502.12110', 'title': 'Paper'},
                {'url': 'https://example.org/news', 'title': 'Outside scope'}])
            out = folder/'out'; out.mkdir()
            with patch('main.build_search', return_value=provider):
                agent = acceptance.reading_recovery_agent(out, None, {}, origin, Mock())
            response = agent.search.search('memory methods')
            self.assertEqual(len(response.results), 1)
            self.assertTrue(provider.search.call_args.args[0].startswith('site:arxiv.org '))
