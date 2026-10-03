"""Exercise the controller with SQLite and the real research workflow, no paid calls."""
import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

from research_agent import FixtureReader, FixtureSearch, ModelDecision, OfflineModel, ResearchAgent
from research_agent.auto_research import authorization
from research_agent.workbench import Workbench
from research_agent.workbench_store import Conflict, NotFound, WorkbenchStore


ROOT = Path(__file__).resolve().parents[1]


def call(name, args, number=1):
    return ModelDecision('tool_call', tool_name=name, arguments=args, call_id=f'call-{number}')


def plan(evidence=None, method='Test the proposed change'):
    return {'title': 'Research proposal', 'baseline': 'Reproduce baseline', 'hypothesis': 'Unverified improvement',
            'method': method, 'validation': 'Same data and evaluator; compare and ablate', 'risks': 'May regress',
            'contributions': ['Hypothesized mechanism'], 'evidence': evidence or []}


def coding(version=1):
    return {'plan_version': version, 'task': 'Implement this version', 'seconds': 30, 'token_budget': 2000,
            'commands': [{'name': role, 'role': role, 'script': 'evaluate.py', 'args': [role],
                          'result_path': role+'.json', 'seconds': 10} for role in ('baseline', 'candidate', 'ablation')]}


class AutoResearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = WorkbenchStore(self.root/'state.db')
        self.space = self.store.save_space({'name': 'Research'})['id']
        self.chat = self.store.create_conversation(self.space, 'Goal')['id']
        self.model = Mock(name='controller')
        self.model.name = 'controller-test'
        self.model.complete = Mock()
        self.app = Workbench(self.store, lambda: self.model, self.agent, self.root/'traces', start_worker=False)
        self.app.coding.work_root = self.root/'project'
        self.app.coding.output_root = self.root/'coding'
        self.addCleanup(self.app.close)

    def agent(self, observer):
        fixture = ROOT/'fixtures/search_results.json'
        return ResearchAgent(FixtureSearch.from_file(fixture), OfflineModel(), self.root/'traces',
                             reader=FixtureReader.from_file(fixture), on_event=observer)

    def start(self, **kwargs):
        return self.app.auto_research.enqueue(self.space, self.chat, 'Study ReAct and run a bounded experiment', **kwargs)

    def step(self):
        self.app.auto_research.tick()
        item = self.store.claim_next()
        if item:
            self.app.execute(item)
        return item

    def state(self, job):
        return self.app.auto_research.state(self.store.job(self.space, job['id']))

    def measured(self, task_id, score):
        # Only the external coding result is simulated here; executor tests exercise process receipts separately.
        self.app.coding.save(task_id, {'summary': 'Candidate implemented', 'measurements': [
            {'name': role, 'role': role, 'valid': True, 'metrics': {'score': value},
             'script_sha256': 'frozen-evaluator', 'metric_origin': 'test fixture', 'result': {
                 'config': {'dataset': 'fixture', 'dataset_version': 'v1', 'split': 'test', 'seeds': [1]}},
             'receipt': {'termination': 'completed', 'exit_code': 0, 'seconds': .1}}
            for role, value in [('baseline', .6), ('candidate', score), ('ablation', .55)]]}, 'completed')

    def test_assess_results_records_target_based_next_step(self):
        job = self.start(targets=[{'name': 'score', 'direction': 'higher', 'value': .8}])
        job = self.store.claim_next()
        state = self.state(job)
        proposal = {**plan(), 'version': 1, 'call_id': 'plan-1'}
        state['plans'] = [proposal]
        request = {k: v for k, v in coding(1).items() if k != 'plan_version'}
        task = self.app.coding.submit(job, 'experiment-1', {**request, 'plan': json.dumps(proposal)})
        state['coding'] = [task['id']]
        self.measured(task['id'], .4)
        config = {'targets': [{'name': 'score', 'direction': 'higher', 'value': .8}]}
        first = self.app.auto_research.assess_results(job, state, config, 1)
        self.assertEqual(first['recommendation'], 'continue_research')
        self.assertEqual(first['next_action'], 'research_or_change_method')
        self.measured(task['id'], .9)
        second = self.app.auto_research.assess_results(job, state, config, 1)
        self.assertEqual(second['recommendation'], 'goal_met')
        self.assertTrue(second['comparable_pairs'][0]['targets'][0]['met'])

        for score, targets, expected in [
            (.7, config['targets'], 'revise_method'),
            (.9, [], 'review_required'),
            (.4, [{'name': 'score', 'direction': 'lower', 'value': .5}], 'goal_met'),
            (.7, [{'name': 'missing_metric', 'direction': 'higher', 'value': 1}], 'continue_research'),
        ]:
            with self.subTest(score=score, targets=targets):
                self.measured(task['id'], score)
                result = self.app.auto_research.assess_results(job, state, {'targets': targets}, 1)
                self.assertEqual(result['recommendation'], expected)
        self.measured(task['id'], .9)
        second_task = self.app.coding.submit(job, 'experiment-2', {**request, 'plan': json.dumps(proposal)})
        state['coding'].append(second_task['id'])
        self.measured(second_task['id'], .7)
        result = self.app.auto_research.assess_results(job, state, config, 1)
        self.assertEqual(result['recommendation'], 'goal_met')
        self.assertEqual(len(result['comparable_pairs']), 2)
        self.assertTrue(all(pair['task_id'] == pair['baseline_task_id'] for pair in result['comparable_pairs']))
        decision = {'tool_name': 'assess_results', 'arguments': {'plan_version': 1}, 'call_id': 'assessment-1'}
        persisted = self.app.auto_research.dispatch(job, state, decision, config)
        self.assertFalse(persisted['scientific_effect_verified'])
        self.app.auto_research.save(job, state)
        self.assertEqual(self.app.auto_research.detail(self.space, job['id'])['assessments'], [persisted])
        state['coding'] = []
        self.assertEqual(self.app.auto_research.dispatch(job, state, decision, config), persisted)
        self.assertEqual(len(state['assessments']), 1)
        self.assertEqual(self.app.auto_research.assess_results(job, state, config, 1)['recommendation'], 'collect_measurements')
        state['coding'] = [task['id']]
        for failure in ('invalid_candidate', 'different_split', 'different_evaluator'):
            self.measured(task['id'], .9)
            data = self.app.coding.get(self.space, task['id'])['state']
            candidate = data['measurements'][1]
            if failure == 'invalid_candidate': candidate['valid'] = False
            if failure == 'different_split': candidate['result']['config']['split'] = 'train'
            if failure == 'different_evaluator': candidate['script_sha256'] = 'changed-evaluator'
            self.app.coding.save(task['id'], data, 'completed')
            with self.subTest(failure=failure):
                assessed = self.app.auto_research.assess_results(job, state, config, 1)
                self.assertEqual(assessed['recommendation'], 'collect_measurements' if failure == 'invalid_candidate' else 'blocked')
                self.assertEqual(assessed['comparable_pairs'], [])

    def test_goal_met_uses_current_assessment_pairing_scope(self):
        job = self.start(targets=[{'name': 'score', 'direction': 'higher', 'value': .8}])
        job = self.store.claim_next()
        state = self.state(job)
        config = json.loads(job['payload'])['auto_research']
        tasks = []
        for version in (1, 2):
            proposal = {**plan(), 'version': version, 'call_id': 'plan-' + str(version)}
            state['plans'].append(proposal)
            request = {k: v for k, v in coding(version).items() if k != 'plan_version'}
            task = self.app.coding.submit(job, 'experiment-' + str(version),
                                          {**request, 'plan': json.dumps(proposal)})
            self.measured(task['id'], .9)
            data = self.app.coding.get(self.space, task['id'])['state']
            data['after'] = {'evaluate.py': 'fixed', 'method.py': str(version)}
            data['measurements'] = [m for m in data['measurements']
                                    if m['role'] == ('baseline' if version == 1 else 'candidate')]
            self.app.coding.save(task['id'], data, 'completed')
            state['coding'].append(task['id'])
            tasks.append(task['id'])
        decision = asdict(call('finish_research', {
            'outcome': 'goal_met', 'summary': 'Claimed target', 'limitations': 'Fixture'}))
        # Same evaluator/data but different plans must not be paired.
        with self.assertRaises(Conflict):
            self.app.auto_research.dispatch(job, state, decision, config)
        request['plan'] = json.dumps(state['plans'][1])
        baseline = self.app.coding.submit(job, 'baseline-v2', request)
        self.measured(baseline['id'], .9)
        data = self.app.coding.get(self.space, baseline['id'])['state']
        data['measurements'] = [data['measurements'][0]]
        data['after'] = {'evaluate.py': 'fixed', 'method.py': 'different-source'}
        self.app.coding.save(baseline['id'], data, 'completed')
        state['coding'].append(baseline['id'])
        # Same plan, separate calls and changed algorithm code are not comparable.
        with self.assertRaises(Conflict):
            self.app.auto_research.dispatch(job, state, decision, config)
        data['after']['method.py'] = '2'
        self.app.coding.save(baseline['id'], data, 'completed')
        assessment = self.app.auto_research.dispatch(job, state,
            asdict(call('assess_results', {'plan_version': 2}, 2)), config)
        self.assertEqual(assessment['recommendation'], 'goal_met')
        result = self.app.auto_research.dispatch(job, state, decision, config)
        self.assertEqual(result['outcome'], 'goal_met')
        state.pop('outcome')
        candidate = self.app.coding.get(self.space, tasks[1])['state']
        candidate['measurements'][0]['valid'] = False
        self.app.coding.save(tasks[1], candidate, 'failed')
        # A saved goal_met assessment cannot override an invalidated receipt.
        with self.assertRaises(Conflict):
            self.app.auto_research.dispatch(job, state, decision, config)
        decision['arguments']['outcome'] = 'reported'
        self.assertEqual(self.app.auto_research.dispatch(job, state, decision, config)['outcome'], 'reported')

    def test_results_drive_research_revision_and_another_coding_delegation(self):
        job = self.start(targets=[{'name': 'score', 'direction': 'higher', 'value': .8}])
        observed = []
        def decide(messages, tools):
            index = len(observed)
            observed.append(messages)
            state = self.state(job)
            if index in (0, 3):
                if index == 3:
                    result = json.loads(messages[-1]['content'])['UNTRUSTED_TOOL_DATA']
                    values = {m['role']: m['metrics']['score'] for m in result['measurements']}
                    self.assertLess(values['candidate'], values['baseline'])
                return call('research', {'question': 'Research ReAct evidence and limitations', 'mode': 'web', 'effort': 'quick'}, index)
            if index in (1, 4):
                result = self.app.auto_research.research_result(job, state['research'][-1])
                self.assertEqual(result['status'], 'completed', result['error'])
                ref = {'job_id': result['id'], 'evidence_id': result['evidence'][0]['evidence_id']}
                return call('save_plan', plan([ref], 'Revised mechanism' if index == 4 else 'Initial mechanism'), index)
            if index in (2, 5):
                return call('coding_agent', coding(1 if index == 2 else 2), index)
            if index == 6:
                return call('assess_results', {'plan_version': 2}, index)
            return call('finish_research', {'outcome': 'goal_met', 'summary': 'Measured target met; novelty remains unverified', 'limitations': 'Small fixture test'}, index)
        self.model.complete.side_effect = decide
        for _ in range(25):
            self.step()
            state = self.state(job)
            if state.get('waiting', {}).get('kind') == 'coding':
                self.measured(state['waiting']['id'], .4 if len(state['coding']) == 1 else .9)
            if self.store.job(self.space, job['id'])['status'] in {'completed', 'failed'}:
                break
        final = self.store.job(self.space, job['id'])
        self.assertEqual(final['status'], 'completed', final['error'])
        detail = self.app.auto_research.detail(self.space, job['id'])
        self.assertEqual(detail['outcome'], 'goal_met')
        self.assertEqual(len(detail['plans']), 2)
        self.assertEqual(len(detail['research']), 2)
        self.assertEqual(len(detail['coding']), 2)
        self.assertIn('Revised mechanism', final['summary'])
        self.assertIn('0.4', final['summary'])
        self.assertEqual(len(observed), 8)

    def test_archive_preserves_scoped_evidence_draft_status_and_retry(self):
        from research_agent.contracts import Claim, Evidence, Source, RunResult
        from research_agent.trace import now_iso
        import hashlib
        job = self.start()
        job = self.store.claim_next()
        state = self.state(job)
        for i in (1, 2):
            child = self.store.enqueue(self.space, self.chat, 'Original ' + str(i), 'Read originals', [],
                payload={'auto_parent': job['id']})
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE research_jobs SET status='running' WHERE id=?", (child['id'],))
            content = 'Original passage from paper ' + str(i)
            original = RunResult('Read original [E1]', [Source('S1', 'Paper '+str(i), f'https://example.org/{i}', '')],
                str(self.root/(child['id']+'.jsonl')), 'ok' if i == 1 else 'insufficient',
                'completed' if i == 1 else 'answer_verification_unavailable', 1, [], [],
                [Evidence('E1', 'S1', content, 'page', content_hash=hashlib.sha256(content.encode()).hexdigest())],
                [Claim('C1', 'Child claim', ['E1'], 'SUPPORTED')])
            run_id = self.store.save(original, 'Read originals', now_iso())
            self.store.finish(child['id'], 'completed' if i == 1 else 'failed', original.answer, run_id=run_id)
            state['research'].append(child['id'])
            state['plans'].append({**plan([{'job_id': child['id'], 'evidence_id': 'E1'}]),
                                   'version': i, 'call_id': 'plan-'+str(i)})
        state.update(outcome='reported', summary='Proposed analysis cites ambiguous [E1]; effect unverified', limitations='Small study')
        self.app.auto_research.save(job, state)
        # Simulate a crash after archival but before the final task/message transaction.
        with patch.object(self.store, 'finish', return_value=False):
            self.app.auto_research.finish(job, state)
        first = self.app.records.report(self.space, job['id'])['versions'][0]
        self.app.records.draft(self.space, job['id'], {'base_version_id': first['id'],
            'content': 'Human correction after interrupted archival', 'note': 'Review correction'})
        self.app.auto_research.finish(job, state)
        final = self.store.job(self.space, job['id'])
        record = self.app.records.report(self.space, job['id'])
        self.assertEqual(len(record['versions']), 2)
        self.assertEqual(record['versions'][-1]['content'], 'Human correction after interrupted archival')
        self.assertEqual(record['versions'][0], first)
        self.assertEqual(first['status'], 'draft')
        self.assertEqual(first['snapshot']['verification'], 'unverified')
        self.assertEqual(first['snapshot']['termination'], 'auto_research_reported')
        self.assertEqual({e['evidence_id'] for e in first['snapshot']['evidence']}, {'E1', 'E2'})
        self.assertEqual({e['source_id'] for e in first['snapshot']['evidence']}, {'S1', 'S2'})
        for e in first['snapshot']['evidence']:
            self.assertEqual(e['provenance']['origin_evidence_id'], 'E1')
            self.assertIn(e['provenance']['origin_job_id'], state['research'])
            self.assertEqual(e['content_hash'], hashlib.sha256(e['content'].encode()).hexdigest())
        self.assertTrue(all(c['status'] == 'HYPOTHESIS' for c in first['snapshot']['claims']))
        self.assertIn('未独立核验', first['content'])
        self.assertIn('子任务引用 E1', first['content'])
        self.assertEqual(self.app.records.coverage(first)['unknown_citations'], [])
        self.assertEqual(self.app.records.coverage(first)['supported_claims'], 0)
        page = self.app.report_file(self.space, job['id'], 'html').read_text(encoding='utf-8')
        self.assertIn('href="#source-E1"', page)
        self.assertIn('id="source-E2"', page)
        self.assertIn('Original passage from paper 2', page)
        self.assertEqual(final['run_id'], first['snapshot']['run_id'])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE job_id=? AND role='assistant' AND intent!='PENDING'", (job['id'],)).fetchone()[0], 1)
        other = self.store.save_space({'name': 'Unrelated'})['id']
        with self.assertRaises(NotFound): self.app.records.report(other, job['id'])

    def test_comparisons_use_host_values_and_refuse_mismatched_conditions(self):
        job = self.start()
        job = self.store.claim_next()
        state = self.state(job)
        for version, score in ((1, .4), (2, .9)):
            proposal = {**plan(method='Method '+str(version)), 'version': version, 'call_id': 'plan-'+str(version)}
            state['plans'].append(proposal)
            request = {k:v for k,v in coding(version).items() if k != 'plan_version'}
            request['plan'] = json.dumps(proposal)
            task = self.app.coding.submit(job, 'experiment-'+str(version), request)
            self.measured(task['id'], score)
            saved = self.app.coding.get(self.space, task['id'])['state']
            saved.update(after={'evaluate.py':'fixed evaluator', 'method.py':str(version)}, summary='Unverified self-test score 999')
            self.app.coding.save(task['id'], saved, 'completed')
            state['coding'].append(task['id'])
        summary = self.app.auto_research.measurement_summary(job, state)
        rows = [r for r in summary['comparisons'] if r['role'] == 'candidate' and r['metric'] == 'score']
        self.assertEqual([r['value'] for r in rows], [.4, .9])
        self.assertAlmostEqual(rows[0]['baseline_delta'], -.2)
        self.assertAlmostEqual(rows[1]['baseline_delta'], .3)
        self.assertAlmostEqual(rows[1]['previous_delta'], .5)
        self.assertNotIn('Unverified self-test score', json.dumps(summary))
        saved['measurements'][1]['result']['config']['split'] = 'different-test'
        self.app.coding.save(task['id'], saved, 'completed')
        row = next(r for r in self.app.auto_research.measurement_summary(job, state)['comparisons']
                   if r['role'] == 'candidate' and r['plan_version'] == 2)
        self.assertIsNone(row['baseline_delta'])
        self.assertIsNone(row['previous_delta'])
        for task_id in state['coding']:
            data = self.app.coding.get(self.space, task_id)['state']
            for m in data['measurements']:
                m['result']['config']['split'] = 'test'
                m['metrics'] = {'score': -1e308 if m['role'] == 'baseline' else 1e308, 'token': 100}
            self.app.coding.save(task_id, data, 'completed')
        overflow = self.app.auto_research.measurement_summary(job, state)
        json.dumps(overflow, allow_nan=False)
        self.assertTrue(all(r['baseline_delta'] is None for r in overflow['comparisons']))
        self.assertGreater(len(overflow['omitted_metrics']), 0)
        self.assertTrue(any(r['comparison_note'] for r in overflow['comparisons']))

    def test_host_measurements_import_into_v3_versions_idempotently(self):
        job = self.start()
        job = self.store.claim_next()
        measurements = []
        for index, (role, score) in enumerate((('baseline', .6), ('candidate', .4), ('ablation', .55))):
            measurements.append({'name': 'v2_' + role, 'role': role, 'valid': True, 'metrics': {'score': score},
                'result': {'config': {'dataset': 'SciFact', 'dataset_version': 'data-hash',
                                       'split': 'claims_dev_cited_doc_ids_only', 'seeds': [13],
                                       'parameters': {'variant': role}}},
                'script_sha256': 'fixed-evaluator', 'source_revision': 'fixed-source',
                'metric_origin': 'host execution', 'task_id': 'task-1', 'measurement_index': index,
                'plan_version': 2, 'command': {'script': 'evaluate.py', 'args': ['--variant', role]}})
        auto = {'measurements': measurements}
        with closing(self.store._connect()) as db, db:
            imported = self.app.records.import_auto_measurements(db, job, auto)
            again = self.app.records.import_auto_measurements(db, job, auto)
        self.assertEqual(len(imported['imported']), 3)
        self.assertEqual(len(again['imported']), 3)
        self.assertEqual(len(self.app.records.experiments(self.space)), 3)
        baseline = next(item for item in imported['imported'] if item['role'] == 'baseline')
        candidate = next(item for item in imported['imported'] if item['role'] == 'candidate')
        comparison = self.app.records.compare_experiments(self.space, baseline['version_id'], candidate['version_id'])
        self.assertTrue(comparison['comparable'])
        self.assertTrue(comparison['pinned_baseline_matches'])
        self.assertEqual(comparison['result_origin'], 'Auto Research 宿主实测；详见各版本 result_source')
        self.assertEqual(comparison['outcome'], 'not_met')

    def test_missing_experiment_conditions_cannot_support_comparisons(self):
        valid = {'dataset': 'fixture', 'dataset_version': 'v1', 'split': 'dev', 'seeds': [1]}
        for invalid in (None, [], {}, {'dataset': None, 'dataset_version': '', 'split': '', 'seeds': []},
                        *[{**valid, field: value} for field, value in (
                            ('dataset', ' '), ('dataset_version', None), ('split', ''), ('seeds', []),
                            ('seeds', '1'), ('seeds', [True]), ('seeds', [1.5]))]):
            with self.subTest(config=invalid):
                record = {'script_sha256': 'same', 'result': {'config': invalid}}
                self.assertFalse(self.app.auto_research.comparable(record, record))
        record = {'script_sha256': 'same', 'result': None}
        self.assertFalse(self.app.auto_research.comparable(record, record))

    def test_structured_dataset_version_supports_comparison(self):
        version_a = {'train': 'sha-train', 'dev': 'sha-dev', 'corpus': 'sha-corpus'}
        version_b = {'corpus': 'sha-corpus', 'dev': 'sha-dev', 'train': 'sha-train'}
        record_a = {'script_sha256': 'same', 'result': {'config': {
            'dataset': 'SciFact', 'dataset_version': version_a,
            'split': 'official_dev_candidates', 'seeds': [13]}}}
        record_b = {'script_sha256': 'same', 'result': {'config': {
            'dataset': 'SciFact', 'dataset_version': version_b,
            'split': 'official_dev_candidates', 'seeds': [13]}}}
        self.assertTrue(self.app.auto_research.comparable(record_a, record_b))
        record_b['result']['config']['dataset_version']['dev'] = 'other'
        self.assertFalse(self.app.auto_research.comparable(record_a, record_b))

    def test_singular_seed_supports_comparison(self):
        def record(value):
            return {'script_sha256': 'same', 'result': {'config': {
                'dataset': 'SciFact', 'dataset_version': {'dev': 'sha-dev'},
                'split': 'dev', 'seed': value}}}
        self.assertTrue(self.app.auto_research.comparable(record(13), record(13)))
        self.assertFalse(self.app.auto_research.comparable(record(13), record(17)))

    def test_researcher_can_measure_existing_code_without_coding_calls(self):
        job = self.start(budget={'coding_calls': 0})
        workspace = self.app.coding.work_root/job['id']
        workspace.mkdir(parents=True)
        (workspace/'evaluate.py').write_text('print("existing experiment")')
        self.model.complete.side_effect = [call('save_plan', plan(), 1),
            call('run_experiments', {'plan_version': 1, 'commands': coding()['commands']}, 2),
            call('finish_research', {'outcome': 'reported', 'summary': 'Existing code measured; candidate regressed', 'limitations': 'Fixture'}, 3)]
        self.step(); self.step()
        state = self.state(job)
        self.assertEqual(state['waiting']['kind'], 'coding')
        task = self.app.coding.get(self.space, state['waiting']['id'])
        self.assertTrue(task['request']['execution_only'])
        self.assertEqual(task['request']['token_budget'], 0)
        self.measured(task['id'], .4)
        self.step()
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'completed')
        messages = self.model.complete.call_args.args[0]
        feedback = json.loads(messages[-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(feedback['measurements'][1]['metrics']['score'], .4)

    def test_failed_experiment_stderr_reaches_research_decision_with_bounded_logs(self):
        job = self.start()
        self.model.complete.side_effect = [call('save_plan', plan(), 1), call('coding_agent', coding(), 2),
            call('finish_research', {'outcome': 'blocked', 'summary': 'Missing dependency', 'limitations': 'Not measured'}, 3)]
        self.step(); self.step()
        task_id = self.state(job)['waiting']['id']
        error = "ModuleNotFoundError: No module named 'sklearn.feature_union'"
        self.app.coding.save(task_id, {'error': 'Experiment failed', 'summary': 'Self-test accuracy 0.99',
            'before': {'method.py': 'unchanged'}, 'after': {'method.py': 'unchanged'},
            'editable_measured_sources': ['method.py'], 'measurements': [{
            'name': 'baseline', 'role': 'baseline', 'valid': False, 'metrics': {},
            'receipt': {'exit_code': 1, 'termination': 'completed', 'seconds': .1,
                        'stdout': 'x' * 8000 + 'before import', 'stderr': 'y' * 8000 + error,
                        'model_error': '宿主模型请求id重复'}}]}, 'failed')
        self.step()
        messages = self.model.complete.call_args.args[0]
        result = json.loads(messages[-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(result['remaining_execution'], self.app.coding.remaining(self.store.job(self.space, job['id'])))
        self.assertEqual(result['summary_origin'], 'coding_agent_self_report_unverified')
        self.assertEqual(result['summary'], 'Self-test accuracy 0.99')
        self.assertFalse(result['host_measurements_available'])
        self.assertEqual(result['source_changes'], [])
        self.assertEqual(result['repairable_files'], ['method.py'])
        self.assertEqual(result['protected_files'], [])
        feedback = result['measurements'][0]
        self.assertFalse(feedback['valid'])
        self.assertEqual(feedback['execution']['exit_code'], 1)
        self.assertTrue(feedback['stderr_tail'].endswith(error))
        self.assertEqual(len(feedback['stderr_tail']), 3000)
        self.assertTrue(feedback['log_tail'].endswith('before import'))
        self.assertEqual(len(feedback['log_tail']), 3000)
        self.assertIn('宿主模型请求id重复', json.dumps(result['measurements'], ensure_ascii=False))

    def test_waiting_coding_does_not_block_other_conversation_or_poll_model(self):
        job = self.start()
        self.model.complete.side_effect = [call('save_plan', plan(), 1), call('coding_agent', coding(), 2)]
        self.step(); self.step()
        self.assertEqual(self.store.job(self.space, job['id'])['stage'], 'auto_waiting')
        self.assertIsNone(self.step())
        self.assertEqual(self.model.complete.call_count, 2)
        other = self.store.create_conversation(self.space, 'Independent')['id']
        self.store.message(self.space, other, 'user', 'ReAct')
        child = self.store.enqueue(self.space, other, 'ReAct', 'ReAct', [], research_effort='quick')
        self.assertEqual(self.step()['id'], child['id'])
        self.assertEqual(self.store.job(self.space, child['id'])['status'], 'completed')
        self.assertEqual(self.model.complete.call_count, 2)

    def test_research_enqueue_is_idempotent_after_crash_before_wait_checkpoint(self):
        job = self.start()
        self.model.complete.return_value = call('research', {'question': 'ReAct', 'mode': 'web', 'effort': 'quick'})
        with patch.object(self.app.auto_research, 'yield_job', side_effect=RuntimeError('injected crash')):
            self.step()
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'failed')
        self.app.auto_research.tick()  # Stops the child while its owner is failed.
        self.app.resume(self.space, job['id'])
        self.step()
        self.assertEqual(self.model.complete.call_count, 1)
        with closing(self.store._connect()) as db:
            children = db.execute("SELECT id FROM research_jobs WHERE json_extract(payload,'$.auto_parent')=?", (job['id'],)).fetchall()
        self.assertEqual(len(children), 1)
        self.assertEqual(self.state(job)['waiting']['id'], children[0]['id'])

    def test_batch_calls_are_serialized_and_persisted(self):
        job = self.start()
        first = call('save_plan', plan())
        first.queued_tool_calls = [asdict(call('coding_agent', coding(), 2))]
        self.model.complete.return_value = first
        self.step(); self.step()
        self.assertEqual(self.model.complete.call_count, 1)
        state = self.state(job)
        self.assertEqual(len(state['plans']), 1)
        self.assertEqual(len(state['coding']), 1)
        self.assertEqual(state['waiting']['kind'], 'coding')

    def test_no_numeric_goal_claim_without_frozen_targets_or_experiments(self):
        job = self.start(budget={'decisions': 1})
        self.model.complete.return_value = call('finish_research', {'outcome': 'goal_met', 'summary': 'Claimed win', 'limitations': ''})
        self.step()
        result = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertFalse(result['ok'])
        self.step()
        self.assertEqual(self.state(job)['outcome'], 'budget_exhausted')
        self.assertNotIn('Claimed win', self.store.job(self.space, job['id'])['summary'])

    def test_cancel_stops_child_and_prevents_new_delegations(self):
        job = self.start()
        self.model.complete.return_value = call('research', {'question': 'ReAct', 'mode': 'web', 'effort': 'quick'})
        self.step()
        child = self.state(job)['waiting']['id']
        self.store.cancel(self.space, job['id'])
        self.assertIsNone(self.step())
        self.assertEqual(self.store.job(self.space, child)['status'], 'cancelled')
        self.assertEqual(self.model.complete.call_count, 1)

    def test_plain_research_and_chat_cannot_gain_coding_capability(self):
        for intent, content in [('CHAT', 'Hello'), ('RESEARCH', 'Survey recent Agent papers')]:
            route = {'intent': intent, 'reply': 'Hello' if intent == 'CHAT' else '',
                     'brief': 'ReAct' if intent == 'RESEARCH' else '', 'assumptions': [], 'effort': 'quick' if intent == 'RESEARCH' else 'none'}
            self.model.complete.return_value = ModelDecision('final', content=json.dumps(route))
            response = self.app.send(self.space, self.chat, content, allow_execution=True)
            self.assertEqual(response['intent'], intent)
            if response.get('job'):
                self.assertNotIn('auto_research', json.loads(response['job']['payload']))
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM coding_tasks').fetchone()[0], 0)

    def test_auto_route_needs_host_capability_in_sync_and_queued_paths(self):
        route = ModelDecision('final', content=json.dumps({'intent': 'AUTO_RESEARCH', 'reply': '', 'brief': 'Bounded goal', 'assumptions': [], 'effort': 'none'}))
        self.model.complete.return_value = route
        refused = self.app.send(self.space, self.chat, 'Automatically implement and experiment', allow_execution=False)
        self.assertEqual(refused['intent'], 'CHAT')
        queued = self.app.send(self.space, self.chat, 'Automatically implement and experiment', background=True, allow_execution=False)
        self.step()
        self.assertEqual(self.store.job(self.space, queued['task_id'])['status'], 'completed')
        self.assertNotIn('auto_research', json.loads(self.store.job(self.space, queued['task_id'])['payload']))
        allowed = self.app.send(self.space, self.chat, 'Automatically implement and experiment', allow_execution=True)
        self.assertEqual(allowed['intent'], 'AUTO_RESEARCH')
        self.assertTrue(json.loads(allowed['job']['payload'])['auto_research']['authorize_execution'])

    def test_budget_and_plan_reference_validation(self):
        for invalid in ({'decisions': True}, {'coding_calls': 10000}, {'unknown': 1}):
            with self.assertRaises(ValueError):
                authorization(invalid)
        job = self.start(budget={'decisions': 1})
        self.model.complete.return_value = call('save_plan', plan([{'job_id': 'outside', 'evidence_id': 'E1'}]))
        self.step()
        self.assertEqual(self.state(job)['plans'], [])
        self.assertFalse(json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']['ok'])

    def test_project_inputs_can_be_inspected_before_first_coding_call(self):
        job = self.start()
        workspace = self.app.coding.work_root/job['id']
        (workspace/'data').mkdir(parents=True)
        (workspace/'data'/'claims.jsonl').write_text('{"claim":"example"}\n', encoding='utf8')
        self.model.complete.side_effect = [call('inspect', {'kind':'files','id':'current','path':'data'}),
            call('inspect', {'kind':'file','id':'current','path':'data/claims.jsonl'}, 2)]
        self.step()
        value = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(value['files'][0]['path'], 'data/claims.jsonl')
        self.step()
        value = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertIn('example', value['content'])
        self.assertEqual(self.state(job)['coding'], [])

    def test_repeated_identical_inspect_is_reported_as_no_progress(self):
        job = self.start(budget={'decisions': 3})
        workspace = self.app.coding.work_root/job['id']
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace/'notes.md').write_text('stable input', encoding='utf-8')
        self.model.complete.side_effect = [
            call('inspect', {'kind': 'file', 'id': 'current', 'path': 'notes.md'}),
            call('inspect', {'kind': 'file', 'id': 'current', 'path': 'notes.md'}, 2),
        ]
        self.step()
        first = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(first['content'], 'stable input')
        self.step()
        second = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertTrue(second['repeated'])
        self.assertEqual(second['no_progress_events'], 1)
        self.assertEqual(self.state(job)['coding'], [])

    def test_same_inspection_refreshes_changed_source_and_reverted_version(self):
        job = self.start(budget={'decisions': 10})
        workspace = self.app.coding.work_root / job['id']
        workspace.mkdir(parents=True)
        target = workspace / 'method.py'
        args = {'kind': 'file', 'id': 'current', 'path': 'method.py'}
        self.model.complete.side_effect = [call('inspect', args, i) for i in range(1, 7)]
        def inspect():
            self.step()
            return json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        target.write_text('first version', encoding='utf-8')
        first = inspect()
        self.assertTrue(inspect()['repeated'])
        target.write_text('other version', encoding='utf-8')  # Same size, different source.
        changed = inspect()
        self.assertEqual(changed.get('content'), 'other version')
        self.assertNotEqual(changed['sha256'], first['sha256'])
        self.assertTrue(inspect()['repeated'])
        self.assertTrue(inspect()['repeated'])
        target.write_text('first version', encoding='utf-8')
        self.assertEqual(inspect().get('content'), 'first version')
        self.assertEqual(self.state(job)['coding'], [])

    def test_failed_inspection_can_retry_after_file_is_available(self):
        job = self.start(budget={'decisions': 4})
        workspace = self.app.coding.work_root / job['id']
        workspace.mkdir(parents=True)
        args = {'kind': 'file', 'id': 'current', 'path': 'later.txt'}
        self.model.complete.side_effect = [call('inspect', args, 1), call('inspect', args, 2)]
        self.step()
        failed = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertFalse(failed['ok'])
        (workspace / 'later.txt').write_text('now available', encoding='utf-8')
        self.step()
        retried = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertEqual(retried.get('content'), 'now available')

    def test_model_feedback_is_bounded_without_changing_input_result(self):
        job = self.start()
        job = self.store.claim_next()
        state = self.state(job)
        state['pending'] = asdict(call('inspect', {'kind': 'file', 'id': 'current', 'path': 'notes.md'}))
        full = {'content': 'x' * 20000, 'evidence': [{'excerpt': 'y' * 4000} for _ in range(20)],
                'metrics': {'score': .4}}
        self.app.auto_research.complete_tool(job, state, full)
        restored = self.app.auto_research.state(job)
        value = json.loads(restored['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertTrue(value['_context_truncated'])
        self.assertLess(len(restored['messages'][-1]['content']), 7000)
        self.assertEqual(len(full['content']), 20000)

    def test_local_previews_and_memory_are_not_original_research_evidence(self):
        for kind, provenance in [('snippet',{}),('local-snippet:D55-1',{}),('local:H12',{}),
                                 ('local:D55',{'artifact_kind':'report'})]:
            with self.subTest(kind=kind):
                self.assertFalse(self.app.auto_research.is_original({'kind':kind,'provenance':provenance}))
        self.assertTrue(self.app.auto_research.is_original({'kind':'local:D55-1','provenance':{'artifact_kind':'github'}}))
        self.assertTrue(self.app.auto_research.is_original({'kind':'page'}))

    def test_large_failed_research_keeps_scoped_original_reference_catalog(self):
        from research_agent.auto_research import AutoResearch
        originals = [{'evidence_id': 'E' + str(i), 'source_id': 'S2', 'kind': 'page',
                      'content_hash': str(i) * 32, 'total_chars': 1800,
                      'job_id': 'child-job', 'excerpt': 'Original paper passage. ' * 100,
                      'provenance': {'page': i}} for i in (15, 16, 17)]
        result = {'id': 'child-job', 'job_id': 'child-job', 'status': 'failed',
                  'summary': 'Unverified draft including historical claims. ' * 220,
                  'error': 'answer_verification_failed', 'evidence': originals,
                  'sources': [{'source_id': 'S2', 'title': 'Paper', 'url': 'https://example.org/paper'}]}
        projected = AutoResearch.context_result(result)
        self.assertEqual(projected['job_id'], 'child-job')
        self.assertEqual([e['evidence_id'] for e in projected['evidence']], ['E15', 'E16', 'E17'])
        self.assertEqual([e['content_hash'] for e in projected['evidence']], [e['content_hash'] for e in originals])
        self.assertEqual(projected['status'], 'failed')
        self.assertEqual(projected['error'], 'answer_verification_failed')
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 7000)
        self.assertTrue(projected['_context_truncated'])
        self.assertEqual(len(result['evidence'][0]['excerpt']), len('Original paper passage. ' * 100))
        result['evidence'] = [dict(originals[i % 3], evidence_id='E' + str(i)) for i in range(50)]
        projected = AutoResearch.context_result(result)
        self.assertEqual(projected['evidence_total'], 50)
        self.assertTrue(0 < len(projected['evidence']) < 50)
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False)), 7000)
        self.assertEqual(projected['evidence'][0]['content_hash'], originals[0]['content_hash'])

    def test_invalid_plan_reference_identifies_only_the_rejected_evidence(self):
        from research_agent.contracts import Evidence, Source, RunResult
        from research_agent.trace import now_iso
        job = self.start()
        job = self.store.claim_next()
        state = self.state(job)
        child = self.store.enqueue(self.space, self.chat, 'Read sources', 'Read sources', [],
                                   payload={'auto_parent': job['id']})
        with closing(self.store._connect()) as db, db:
            db.execute("UPDATE research_jobs SET status='running' WHERE id=?", (child['id'],))
        original = RunResult('Draft remains unverified', [Source('S1', 'Source', 'https://example.org/paper', '')],
            str(self.root/'child.jsonl'), 'insufficient', 'answer_verification_failed', 1, [], [], [
                Evidence('E9', 'S1', 'Old conversation', 'local:H1', provenance={'section': 'history'}),
                Evidence('E10', 'S1', 'Memory summary', 'local:D1', provenance={'artifact_kind': 'report'}),
                Evidence('E11', 'S1', 'Search preview', 'snippet'),
                Evidence('E15', 'S1', 'Original method', 'page'),
                Evidence('E17', 'S1', 'Original setting', 'local:D2-0', provenance={'artifact_kind': 'paper'}),
            ], [])
        run_id = self.store.save(original, 'Read sources', now_iso())
        self.store.finish(child['id'], 'failed', original.answer, run_id=run_id)
        state['research'].append(child['id'])
        valid = [{'job_id': child['id'], 'evidence_id': eid} for eid in ('E15', 'E17')]
        self.assertEqual([e['evidence_id'] for e in self.app.auto_research.research_result(job, child['id'])['evidence']],
                         ['E15', 'E17'])
        for rejected in ('E9', 'E10', 'E11', 'E404'):
            with self.subTest(rejected=rejected):
                refs = [{'job_id': child['id'], 'evidence_id': rejected}, *valid]
                with self.assertRaises(ValueError) as failure:
                    self.app.auto_research.dispatch(job, state, asdict(call('save_plan', plan(refs))), {})
                self.assertIn(child['id'] + '/' + rejected, str(failure.exception))
                self.assertIn('其他有效原文引用可保留', str(failure.exception))
                self.assertEqual(state['plans'], [])
        result = self.app.auto_research.dispatch(job, state, asdict(call('save_plan', plan(valid), 2)), {})
        self.assertEqual(result['status'], 'HYPOTHESIS')
        self.assertEqual(state['plans'][0]['evidence'], valid)

    def test_http_server_starts_both_research_and_coding_workers(self):
        from server import make_server
        server = make_server('127.0.0.1', 0, db_path=self.root/'server.sqlite', trace_dir=self.root/'server-traces',
                             model_factory=lambda: self.model, agent_factory=self.agent, start_worker=True)
        try:
            self.assertTrue(server.app.worker.is_alive())
            self.assertTrue(server.app.coding.thread.is_alive())
        finally:
            server.app.close()
            server.server_close()

    def test_missing_project_file_returns_correctable_tool_error(self):
        job = self.start()
        self.model.complete.side_effect = [call('inspect', {'kind': 'file', 'id': 'current', 'path': 'missing.py'}),
            call('finish_research', {'outcome': 'reported', 'summary': 'Project input missing', 'limitations': 'No experiment'}, 2)]
        self.step()
        result = json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertFalse(result['ok'])
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'queued')
        self.step()
        self.assertEqual(self.state(job)['outcome'], 'reported')

    def test_explicit_parent_resume_queues_completed_coding_receipt_without_new_model_call(self):
        job = self.start()
        self.model.complete.side_effect = [call('save_plan', plan()), call('coding_agent', coding(), 2)]
        self.step(); self.step()
        task_id = self.state(job)['waiting']['id']
        receipt = self.app.coding.output_root/task_id
        receipt.mkdir(parents=True)
        (receipt/'codex.json').write_text(json.dumps({'termination': 'completed', 'exit_code': 0,
            'stdout': json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 20}})}))
        self.app.coding.save(task_id, {'coding_started': 'before-interruption'}, 'interrupted')
        self.app.auto_research.tick()
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'interrupted')
        self.app.resume(self.space, job['id'])
        self.step()
        self.assertEqual(self.app.coding.get(self.space, task_id)['status'], 'queued')
        self.assertEqual(self.store.job(self.space, job['id'])['stage'], 'auto_waiting')
        self.assertEqual(self.model.complete.call_count, 2)

    def test_parent_resume_refuses_unknown_coding_execution(self):
        job = self.start()
        self.model.complete.side_effect = [call('save_plan', plan()), call('coding_agent', coding(), 2)]
        self.step(); self.step()
        task_id = self.state(job)['waiting']['id']
        self.app.coding.save(task_id, {'coding_started': 'before-interruption'}, 'interrupted')
        self.app.auto_research.tick()
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'interrupted')
        self.app.resume(self.space, job['id'])
        self.step()
        self.assertEqual(self.app.coding.get(self.space, task_id)['status'], 'interrupted')
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'failed')
        self.assertIn('收据缺失', self.store.job(self.space, job['id'])['error'])
        self.assertEqual(self.model.complete.call_count, 2)

    def test_long_tool_feedback_remains_parseable_after_checkpoint(self):
        self.start()
        job = self.store.claim_next()
        state = self.app.auto_research.state(job)
        state['pending'] = asdict(call('inspect', {'kind': 'file', 'id': 'current', 'path': 'source.py'}))
        self.app.auto_research.complete_tool(job, state, {'id': 'tool', 'content': 'x' * 18000, 'metrics': {'score': .4}})
        restored = self.app.auto_research.state(job)
        value = json.loads(restored['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']
        self.assertTrue(value['_context_truncated'])
        self.assertLess(len(restored['messages'][-1]['content']), 7000)
        self.assertEqual(value['metrics']['score'], .4)

    def test_result_context_keeps_nested_metric_and_config_contract(self):
        compact = self.app.auto_research.context_result({'id': 'coding', 'measurements': [{
            'name': 'candidate', 'role': 'candidate', 'valid': True,
            'metrics': {'recall_at_5': .5, 'mrr_at_5': .25},
            'result': {'config': {'dataset': 'qasper', 'dataset_version': {'tasks': 'hash'},
                                  'split': 'dev', 'seeds': [13, 17, 29], 'parameters': {'method': 'bm25'}},
                       'diagnostics': {'per_seed': [{'seed': 13, 'metrics': {'recall_at_5': .5}}]}}
        }]})
        measurement = compact['measurements'][0]
        self.assertEqual(measurement['result']['config']['dataset_version'], {'tasks': 'hash'})
        self.assertEqual(measurement['result']['config']['parameters'], {'method': 'bm25'})
        self.assertEqual(measurement['result']['diagnostics']['per_seed'][0]['metrics']['recall_at_5'], .5)

    def test_result_context_bounds_large_parameter_contract(self):
        compact = self.app.auto_research.context_result({'id': 'coding', 'measurements': [{
            'name': 'candidate', 'role': 'candidate', 'valid': True, 'metrics': {'score': .5},
            'result': {'config': {'dataset': 'project', 'parameters': {str(i): 'x' * 200 for i in range(2000)}}}
        }]})
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=False)), 7000)
        self.assertTrue(compact.get('_context_truncated'))

    def test_result_context_keeps_identifiable_failure_examples_under_large_diagnostics(self):
        example = {'task_id': 'conv-26/qa-052', 'seed': 13, 'category': 1,
                   'conversation_id': 'conv-26', 'metrics': {'answer_f1': 0}}
        compact = self.app.auto_research.context_result({'measurements': [{
            'name': 'baseline', 'role': 'baseline', 'valid': True, 'metrics': {'answer_f1': .4},
            'failure_examples': [example],
            'metric_audit': {'per_task': [example], 'per_category': [{'category': 1, 'metrics': {'answer_f1': .4}}]},
            'result': {'config': {'dataset': 'locomo'}, 'diagnostics': {str(i): 'x' * 5000 for i in range(30)}}
        }]})
        self.assertEqual(compact['measurements'][0]['failure_examples'], [example])
        self.assertLess(len(json.dumps(compact)), 7000)

    def test_saved_model_response_resumes_without_another_paid_request(self):
        job = self.start(budget={'decisions': 1})
        self.model.complete.return_value = call('save_plan', plan())
        save = self.app.auto_research.save
        def interrupted(item, state):
            if state.get('pending'):
                raise RuntimeError('crash after response receipt, before pending decision checkpoint')
            return save(item, state)
        with patch.object(self.app.auto_research, 'save', side_effect=interrupted):
            self.step()
        self.assertTrue(self.state(job)['model_inflight'])
        self.app.resume(self.space, job['id'])
        self.step()
        self.assertEqual(self.model.complete.call_count, 1)
        self.assertEqual(len(self.state(job)['plans']), 1)

    def test_unknown_inflight_response_never_replays_on_resume(self):
        job = self.start()
        self.model.complete.side_effect = TimeoutError('response missing')
        self.step()
        self.app.resume(self.space, job['id'])
        self.step()
        self.assertEqual(self.model.complete.call_count, 1)
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'failed')
        self.assertIn('收据缺失', self.store.job(self.space, job['id'])['error'])

    def test_invalidated_memory_does_not_reset_state_or_restart_research(self):
        job = self.start()
        self.model.complete.return_value = call('save_plan', plan())
        self.step()
        with closing(self.store._connect()) as db, db:
            self.app.memory.invalidate(db, self.space)
        self.step()
        self.assertEqual(self.model.complete.call_count, 1)
        self.assertEqual(len(self.state(job)['plans']), 1)
        self.assertEqual(self.store.job(self.space, job['id'])['status'], 'failed')

    def test_different_split_or_evaluator_cannot_prove_numeric_target(self):
        job = self.start(budget={'decisions': 3}, targets=[{'name': 'score', 'direction': 'higher', 'value': .8}])
        self.model.complete.side_effect = [call('save_plan', plan()), call('coding_agent', coding(), 2),
            call('finish_research', {'outcome': 'goal_met', 'summary': 'Claimed incomparable win', 'limitations': ''}, 3)]
        self.step(); self.step()
        task = self.state(job)['waiting']['id']
        self.measured(task, .99)
        data = self.app.coding.get(self.space, task)['state']
        data['measurements'][1]['result']['config']['split'] = 'train'
        data['measurements'][1]['script_sha256'] = 'changed-evaluator'
        self.app.coding.save(task, data, 'completed')
        self.step()
        self.assertFalse(json.loads(self.state(job)['messages'][-1]['content'])['UNTRUSTED_TOOL_DATA']['ok'])
        self.step()
        self.assertEqual(self.state(job)['outcome'], 'budget_exhausted')


if __name__ == '__main__':
    unittest.main()
