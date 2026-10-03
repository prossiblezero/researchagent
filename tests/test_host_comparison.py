"""Fixed host scoring must support baseline -> new plan -> separate method scripts."""
import copy
import hashlib
import json
from contextlib import closing
from pathlib import Path
import unittest

import tests.test_auto_research as fixtures
from research_agent.locomo_metrics import audit_locomo_result
from evals.run_auto_research_acceptance import method_iteration_evidence


class HostComparisonTests(unittest.TestCase):
    setUp = fixtures.AutoResearchTests.setUp
    agent = fixtures.AutoResearchTests.agent
    state = fixtures.AutoResearchTests.state

    def setup_run(self):
        workspace = self.root/'scoring'; workspace.mkdir()
        tasks = [{'id': 'q1', 'conversation_id': 'c1', 'split': 'development', 'category': 1}]
        corpus = [{'conversation_id': 'c1', 'split': 'development', 'conversation': {'session_1': [{'dia_id': 'D1:1'}]}}]
        labels = [{'id': 'q1', 'answer': 'Paris', 'gold_evidence': ['D1:1'], 'retrieval_scorable': True}]
        contract = {'name': 'locomo_qa_v1', 'seeds': [13], 'tasks_path': 'tasks.json', 'corpus_path': 'corpus.json',
                    'frozen_sources': {'baseline.py': hashlib.sha256(b'# frozen baseline').hexdigest()}}
        for name, values in [('tasks', tasks), ('corpus', corpus), ('labels', labels)]:
            raw = json.dumps(values).encode(); (workspace/(name+'.json')).write_bytes(raw)
            contract[name+'_sha256'] = hashlib.sha256(raw).hexdigest()
        job = self.app.auto_research.enqueue(self.space, self.chat, 'Improve the memory method',
            targets=[{'name': 'answer_f1', 'direction': 'higher', 'value': .8}], metric_contract=contract)
        job = self.store.claim_next(); state = self.state(job)
        project = self.app.coding.work_root/job['id']; project.mkdir(parents=True)
        (project/'baseline.py').write_bytes(b'# frozen baseline')
        for name in ('tasks.json', 'corpus.json'):
            (project/name).write_bytes((workspace/name).read_bytes())
        state['plans'] = [{**fixtures.plan(method='method '+str(v)), 'version': v, 'call_id': 'plan-'+str(v)} for v in (1, 2)]
        tasks_out = []
        for version, roles in [(1, ['baseline']), (2, ['candidate', 'ablation'])]:
            request = {k: v for k, v in fixtures.coding(version).items() if k!='plan_version'}
            request.update(plan=json.dumps(state['plans'][version-1]),
                model_requests={'mode':'stdio','model_id':'default','max_requests':10,'seconds':60})
            task = self.app.coding.submit(job, 'measured-'+str(version), request)
            measurements = []
            for index, role in enumerate(roles):
                predictions = [{'task_id':'q1','seed':13,'answer':'Paris' if role=='candidate' else 'London','evidence_ids':['D1:1']}]
                prediction = (workspace/'predictions.jsonl'); prediction.write_text(json.dumps(predictions[0])+'\n')
                raw_result = {'metrics':{'invented_score':1}, 'config':{'dataset':'locomo','dataset_version':contract['tasks_sha256'],
                    'split':'development','seeds':[13]}, 'diagnostics':{'predictions_path':'predictions.jsonl'}}
                result, audit = audit_locomo_result(workspace, raw_result, contract, workspace/'labels.json')
                measurements.append({'name':role,'role':role,'valid':True,'metrics':result['metrics'],'result':result,
                    'script_sha256':hashlib.sha256(role.encode()).hexdigest(), 'metric_audit':audit,
                    'host_model_request_range':[index, index+1],
                    'prediction_artifacts':{'predictions_path':{'sha256':audit['predictions_sha256']}},
                    'metric_origin':'host-audited fixed dataset contract; raw script output preserved in result artifact',
                    'receipt':{'termination':'completed','exit_code':0,'seconds':1}})
            self.app.coding.save(task['id'], {'after':{'baseline.py':'f'*64, **({'candidate.py':'new-method'} if version==2 else {})},
                                              'measurements':measurements, 'model_requests':[{'valid':True, 'model_id':'default',
                                                'usage_records':[{'model':'fixture-luna','error':None}]} for _ in roles]}, 'completed')
            state['coding'].append(task['id']); tasks_out.append(self.app.coding.get(self.space, task['id']))
        return job, state, tasks_out

    def test_new_plan_compares_separate_scripts_using_earlier_host_scored_baseline(self):
        job, state, tasks = self.setup_run()
        result = self.app.auto_research.assess_results(job, state, {'targets':[]}, 2)
        self.assertEqual(result['recommendation'], 'review_required')
        pair = result['comparable_pairs'][0]
        self.assertEqual(pair['baseline_task_id'], tasks[0]['id'])
        self.assertEqual(pair['candidate_metrics']['answer_f1'], 1)
        summary = self.app.auto_research.measurement_summary(job, state)
        row = next(r for r in summary['comparisons'] if r['role']=='candidate' and r['metric']=='answer_f1')
        self.assertEqual(row['baseline_delta'], 1)
        with closing(self.store._connect()) as db, db:
            imported = self.app.records.import_auto_measurements(db, job, summary)
        baseline = next(r for r in imported['imported'] if r['role']=='baseline')
        candidate = next(r for r in imported['imported'] if r['role']=='candidate')
        compared = self.app.records.compare_experiments(self.space, baseline['version_id'], candidate['version_id'])
        self.assertTrue(compared['comparable'])
        self.assertTrue(compared['pinned_baseline_matches'])
        self.assertEqual(next(m for m in compared['metrics'] if m['name']=='answer_f1')['delta'], 1)

    def test_cross_plan_report_identifies_the_selected_baseline(self):
        job, state, tasks = self.setup_run()
        summary = self.app.auto_research.measurement_summary(job, state)
        row = next(r for r in summary['comparisons'] if r['role'] == 'candidate' and r['metric'] == 'answer_f1')
        self.assertEqual(row.get('baseline_plan_version'), 1)
        self.assertEqual(row['plan_version'], 2)
        for plan in state['plans']:
            plan['evidence'] = []
        state.update(outcome='reported', summary='Cross-plan fixture comparison', limitations='Fixture only')
        self.app.auto_research.finish(job, state)
        answer = self.store.job(self.space, job['id'])['summary']
        self.assertIn('| 基线来源 | 配对基线 |', answer)
        self.assertNotIn('同版基线', answer)
        candidate_line = next(line for line in answer.splitlines() if '/candidate | answer_f1 |' in line)
        self.assertIn('| 1/' + tasks[0]['id'][:8] + ' | 0 | 1 |', candidate_line)
        self.assertIn('| 2 | ' + tasks[1]['id'][:8] + '/candidate |', candidate_line)

    def test_first_method_revision_can_follow_baseline_feedback(self):
        job, state, tasks = self.setup_run()
        request = {'purpose':'auto_research','request_hash':'fixture','response':{
            'kind':'tool_call','tool_name':'save_plan','call_id':'plan-2'},
            'messages':[{'role':'tool','content':json.dumps({'UNTRUSTED_TOOL_DATA':{
                'id':tasks[0]['id'],'measurements':tasks[0]['state']['measurements']}})}]}
        result = method_iteration_evidence([request], tasks, state['plans'])
        self.assertTrue(result['structural_path_completed'])
        self.assertEqual(result['iterations'][0]['prior_plan_version'], 1)
        self.assertIn(tasks[0]['id'], result['iterations'][0]['comparison_tasks'])


    def test_first_revision_cannot_skip_an_existing_candidate_in_another_task(self):
        _, state, tasks = self.setup_run()
        old_candidate = copy.deepcopy(tasks[1])
        old_candidate['id'] = 'old-candidate'
        old_candidate['request']['plan'] = json.dumps(state['plans'][0])
        old_candidate['updated_at'] = '2026-10-01T01:00:00+00:00'
        tasks.append(old_candidate)
        feedback = {'id': tasks[0]['id'], 'status': 'completed',
                    'measurements': tasks[0]['state']['measurements']}
        request = {'purpose': 'auto_research', 'started_at': '2026-10-01T02:00:00+00:00',
            'response': {'kind': 'tool_call', 'tool_name': 'save_plan', 'call_id': 'plan-2'},
            'messages': [{'role': 'tool', 'content': json.dumps({'UNTRUSTED_TOOL_DATA': feedback})}]}
        self.assertFalse(method_iteration_evidence([request], tasks, state['plans'])['structural_path_completed'])
        # A result completed only after this decision cannot change the historical decision.
        old_candidate['updated_at'] = '2026-10-01T03:00:00+00:00'
        self.assertTrue(method_iteration_evidence([request], tasks, state['plans'])['structural_path_completed'])
        old_candidate['updated_at'] = '2026-10-01T01:00:00+00:00'
        feedback = {'id': old_candidate['id'], 'status': 'completed',
                    'measurements': old_candidate['state']['measurements']}
        request['messages'].append({'role': 'tool', 'content': json.dumps({'UNTRUSTED_TOOL_DATA': feedback})})
        old_candidate['state']['after'] = copy.deepcopy(tasks[0]['state']['after'])
        self.assertTrue(method_iteration_evidence([request], tasks, state['plans'])['structural_path_completed'])

    def test_host_split_identity_ignores_script_reported_split_hash(self):
        job, state, tasks = self.setup_run()
        changed = copy.deepcopy(tasks[1]['state'])
        changed['measurements'][0]['result']['config']['split_hash'] = 'script-claimed-hash'
        self.app.coding.save(tasks[1]['id'], changed, 'completed')
        summary = self.app.auto_research.measurement_summary(job, state)
        with closing(self.store._connect()) as db, db:
            imported = self.app.records.import_auto_measurements(db, job, summary)
        baseline = next(m for m in imported['imported'] if m['role'] == 'baseline')
        candidate = next(m for m in imported['imported'] if m['role'] == 'candidate')
        result = self.app.records.compare_experiments(self.space, baseline['version_id'], candidate['version_id'])
        self.assertTrue(result['comparable'], result)

    def test_host_conditions_and_actual_model_are_required_even_with_same_script(self):
        _, _, tasks = self.setup_run()
        controller = self.app.auto_research
        baseline = controller.measurements_for_comparison(tasks[0])[0]
        for change in ('labels', 'frozen_source', 'prediction', 'metrics', 'no_audit', 'no_actual_model',
                       'different_actual_model', 'mixed_models', 'empty_model_range', 'wrong_model_id', 'different_job'):
            with self.subTest(change=change):
                task = copy.deepcopy(tasks[1]); measurement = task['state']['measurements'][0]
                measurement['script_sha256'] = baseline['script_sha256']
                if change == 'labels': task['request']['metric_contract']['labels_sha256'] = '0'*64
                if change == 'frozen_source': task['request']['metric_contract']['frozen_sources']['baseline.py'] = '0'*64
                if change == 'prediction': measurement['prediction_artifacts']['predictions_path']['sha256'] = '0'*64
                if change == 'metrics': measurement['metrics']['answer_f1'] = .7
                if change == 'no_audit': measurement.pop('metric_audit')
                if change == 'no_actual_model': task['state']['model_requests'][0]['usage_records'] = []
                if change == 'different_actual_model': task['state']['model_requests'][0]['usage_records'][0]['model'] = 'other-model'
                if change == 'mixed_models': task['state']['model_requests'][0]['usage_records'].append({'model':'other-model'})
                if change == 'empty_model_range': measurement['host_model_request_range'] = [0, 0]
                if change == 'wrong_model_id': task['state']['model_requests'][0]['model_id'] = 'other'
                if change == 'different_job': task['job_id'] = 'other-job'
                candidate = controller.measurements_for_comparison(task)[0]
                self.assertFalse(controller.comparable(candidate, baseline))

    def test_ambiguous_baselines_are_not_selected_by_score(self):
        job, state, tasks = self.setup_run()
        previous = copy.deepcopy(tasks[0]['state'])
        duplicate = copy.deepcopy(previous['measurements'][0]); duplicate['name'] = 'another-baseline'
        previous['measurements'].append(duplicate)
        self.app.coding.save(tasks[0]['id'], previous, 'completed')
        assessment = self.app.auto_research.assess_results(job, state, {'targets':[]}, 2)
        self.assertEqual(assessment['recommendation'], 'blocked')
        self.assertEqual(assessment['comparable_pairs'], [])

    def test_v3_import_retains_multiple_candidates_and_reuses_imported_baseline(self):
        job, state, _ = self.setup_run()
        summary = self.app.auto_research.measurement_summary(job, state)
        rows = summary['measurements']
        extra = copy.deepcopy(next(m for m in rows if m['role']=='candidate'))
        extra['measurement_index'] = 9; extra['name'] = 'second-candidate'
        rows.append(extra)
        with closing(self.store._connect()) as db, db:
            first = self.app.records.import_auto_measurements(db, job, {'measurements':[rows[0]]})
            baseline_id = first['imported'][0]['version_id']
            imported = self.app.records.import_auto_measurements(db, job, summary)
            repeated = self.app.records.import_auto_measurements(db, job, summary)
        self.assertEqual(len(imported['imported']), 4)
        self.assertEqual([m['version_id'] for m in imported['imported']], [m['version_id'] for m in repeated['imported']])
        self.assertTrue(all(m['baseline_version_id']==baseline_id for m in imported['imported'] if m['role']!='baseline'))
        self.assertEqual(len(self.app.records.experiments(self.space)), 4)


    def test_v3_does_not_fall_back_when_host_contract_provenance_is_missing(self):
        job, state, tasks = self.setup_run()
        merged = copy.deepcopy(tasks[0]['state'])
        merged['measurements'].extend(copy.deepcopy(tasks[1]['state']['measurements']))
        merged['model_requests'] = []
        self.app.coding.save(tasks[0]['id'], merged, 'completed')
        empty = copy.deepcopy(tasks[1]['state']); empty['measurements'] = []
        self.app.coding.save(tasks[1]['id'], empty, 'completed')
        summary = self.app.auto_research.measurement_summary(job, state)
        self.assertTrue(all(m['comparison_contract'] is None for m in summary['measurements']))
        with closing(self.store._connect()) as db, db:
            imported = self.app.records.import_auto_measurements(db, job, summary)
        baseline = next(m for m in imported['imported'] if m['role']=='baseline')
        candidate = next(m for m in imported['imported'] if m['role']=='candidate')
        result = self.app.records.compare_experiments(self.space, baseline['version_id'], candidate['version_id'])
        self.assertFalse(result['comparable'])
        self.assertIn('host_scoring_contract', result['mismatches'])

    def test_independent_assessment_checks_the_same_cross_plan_pair(self):
        from evals.auto_research_assessment import assessment_evidence
        job, state, tasks = self.setup_run()
        result = self.app.auto_research.assess_results(job, state, {'targets':[]}, 2)
        result['call_id'] = 'assess-2'
        request = {'purpose':'auto_research','response':{'kind':'tool_call','tool_name':'assess_results',
                   'call_id':'assess-2','arguments':{'plan_version':2}},
                   'messages':[{'role':'tool','tool_call_id':'result-'+task['id'],
                       'content':json.dumps({'UNTRUSTED_TOOL_DATA':{'id':task['id'], 'status':'completed',
                                           'measurements':task['state']['measurements']}})} for task in tasks]}
        following = {'purpose':'auto_research','response':{'kind':'tool_call','tool_name':'finish_research'},
            'messages':[{'role':'tool','tool_call_id':'assess-2','content':json.dumps({'UNTRUSTED_TOOL_DATA':result})}]}
        checked = assessment_evidence([request,following], tasks, [result], [])
        self.assertTrue(checked['passed'], checked)
        broken = copy.deepcopy(result); broken['comparable_pairs'][0]['baseline_metrics']['answer_f1'] = .5
        self.assertFalse(assessment_evidence([request,following], tasks, [broken], [])['passed'])
        future = copy.deepcopy(tasks[0]); future['id'] = 'future-baseline'
        future['updated_at'] = '2026-10-01T03:00:00+00:00'
        request['started_at'] = '2026-10-01T02:00:00+00:00'
        # Later same-plan baselines must not retroactively invalidate this assessment.
        self.assertTrue(assessment_evidence([request, following], [*tasks, future], [result], [])['passed'])
        future_candidate = copy.deepcopy(tasks[1]); future_candidate['id'] = 'future-candidate'
        future_candidate['updated_at'] = '2026-10-01T03:00:00+00:00'
        invented = copy.deepcopy(result)
        extra_pair = copy.deepcopy(result['comparable_pairs'][0]); extra_pair['task_id'] = future_candidate['id']
        invented['comparable_pairs'].append(extra_pair)
        self.assertFalse(assessment_evidence([request, following], [*tasks, future_candidate], [invented], [])['passed'])
        # An already completed baseline makes the selection ambiguous even if not shown to the LLM.
        future['updated_at'] = '2026-10-01T01:00:00+00:00'
        self.assertFalse(assessment_evidence([request, following], [*tasks, future], [result], [])['passed'])



if __name__ == '__main__':
    unittest.main()
