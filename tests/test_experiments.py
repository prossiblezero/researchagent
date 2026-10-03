"""V4 execution contracts; no model calls in regression tests."""
import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from research_agent.workbench import Workbench, OfflineRouter
from research_agent.workbench_store import WorkbenchStore, Conflict, NotFound
from research_agent.experiment_retrieval import BASELINE, configuration, evaluate, frozen_data, sha
from research_agent.experiment_process import coding_command, permissions, clean_environment


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkbenchStore(self.root / 'state.db')
        self.app = Workbench(self.store, OfflineRouter, lambda *a: None, self.root / 'traces', start_worker=False)
        self.addCleanup(self.app.close)
        self.executor = self.app.experiments; self.executor.work_root = self.root / 'workspaces'
        self.space = self.store.save_space({'name': 'V4'})['id']
        self.chat = self.store.create_conversation(self.space, 'experiment')['id']
        self.plan = self.executor.prepare(self.space, {'hypothesis': '验证分词调整是否有用', 'suite': 'public-retrieval-v1'})
        self.version = self.plan['versions'][-1]['id']
        self.body = {'conversation_id': self.chat, 'authorize_execution': True, 'seconds': 60, 'token_budget': 2000}

    def submit(self):
        with patch('research_agent.experiments.codex_path', return_value='codex.exe'):
            return self.executor.submit(self.space, self.version, self.body)

    def test_authorization_budget_duplicate_and_scoped_access(self):
        for updates in ({'authorize_execution': False}, {'seconds': True}, {'seconds': 601}, {'token_budget': 1}, {'command': 'anything'}):
            with self.assertRaises(ValueError): self.executor.submit(self.space, self.version, {**self.body, **updates})
        run = self.submit()
        with self.assertRaises(Conflict): self.submit()
        other = self.store.save_space({'name': 'other'})['id']
        with self.assertRaises(NotFound): self.executor.get(other, run['job_id'])
        with self.assertRaises(NotFound): self.executor.artifact(self.space, run['job_id'], '../state.db')
        self.assertEqual(run['contract']['baseline']['id'], self.plan['versions'][0]['baseline_version_id'])
        self.assertEqual(run['job']['kind'], 'EXPERIMENT')

    def test_changed_baseline_or_units_cannot_execute(self):
        version = self.plan['versions'][0]; content = copy.deepcopy(version['content'])
        body = {k: v for k, v in content.items() if k not in ('executed_by_workbench', 'measurement_status')}
        body.update(base_version_id=self.version, baseline_version_id=version['baseline_version_id'])
        body['metrics'][0]['unit'] = 'faked unit'
        changed = self.app.records.save_experiment(self.space, body, self.plan['id'])
        with patch('research_agent.experiments.codex_path', return_value='codex.exe'):
            with self.assertRaises(Conflict): self.executor.submit(self.space, self.version, self.body)
            with self.assertRaises(ValueError): self.executor.submit(self.space, changed['versions'][-1]['id'], self.body)

    def test_trusted_scoring_rejects_faked_metrics_and_duplicate_predictions(self):
        data, _ = frozen_data()
        def evaluate_stdout(stdout, termination='completed'):
            runner=lambda *a, **kw: {'stdout': stdout, 'stderr': '', 'termination': termination, 'exit_code': 0, 'seconds': 1}
            with patch('research_agent.experiment_retrieval.sandbox_command', return_value=['sandbox']):
                return evaluate(self.root, data, lambda: False, runner=runner)
        for output in ('{"recall_at5":100}', json.dumps([['docling:1', 'docling:1']] * 60), 'NaN'):
            self.assertFalse(evaluate_stdout(output)['valid'])
        self.assertFalse(evaluate_stdout('[]', 'timeout')['valid'])
        hits = [[f"{g['document']}:{g['ordinal']}" for g in q['gold']] for q in data['questions']]
        result = evaluate_stdout(json.dumps(hits))
        expected=sum(min(5,len(q['gold']))/len(q['gold'])*100 for q in data['questions'] if q['split']=='held_out' and q['gold'])/30
        self.assertTrue(result['valid']); self.assertAlmostEqual(result['metrics']['recall_at5'], expected)
        self.assertEqual(result['answerable_test'], 30)

    def test_atomic_result_versions_idempotent_and_cancel_fenced(self):
        run = self.submit(); job = self.store.claim_next()
        metrics = {'recall_at5': 30., 'mrr': .2, 'latency_ms': 4.}
        state = {'phase': 'comparing', 'code_hash': sha(BASELINE), 'baseline': {'metrics': metrics}, 'candidate': {'metrics': metrics}}
        first = self.executor.commit(job, run['contract'], state)
        self.assertEqual(first, self.executor.commit(job, run['contract'], state))
        compare = self.app.records.compare_experiments(self.space, first[1], first[0])
        self.assertEqual(compare['outcome'], 'not_met')
        self.assertTrue(compare['pinned_baseline_matches'])
        self.assertTrue(compare['right']['content']['executed_by_workbench'])
        original = self.app.records.experiment(self.space, self.plan['id'])['versions'][0]
        self.assertEqual(original['id'], self.version); self.assertEqual(original['content']['status'], 'planned')
        self.assertEqual(self.store.job(self.space,job['id'])['status'],'completed')
        with self.assertRaises(Conflict):self.store.cancel(self.space, job['id'])
        other=self.executor.prepare(self.space,{'hypothesis':'cancellation case','suite':'public-retrieval-v1'});self.version=other['versions'][-1]['id']
        second=self.submit();second_job=self.store.claim_next();self.store.cancel(self.space,second_job['id'])
        with self.assertRaises(Conflict):self.executor.commit(second_job,second['contract'],state)

    def test_cancel_and_stale_generation_cannot_save_checkpoint(self):
        run = self.submit(); job = self.store.claim_next()
        with closing(self.store._connect()) as db, db:
            db.execute('UPDATE research_jobs SET execution_generation=1 WHERE id=?', (job['id'],))
        with self.assertRaises(Conflict): self.executor.save(job, run['state'])
        self.store.cancel(self.space, job['id'])
        with self.assertRaises(Conflict): self.executor.save({**job, 'execution_generation': 1}, run['state'])

    def test_coding_failure_cannot_spend_budget_again_on_resume(self):
        run = self.submit(); job = self.store.claim_next()
        self.executor.save(job, {'phase': 'coding', 'coding_invocations': 1, 'evaluation_attempts': 1})
        self.store.finish(job['id'], 'failed', 'coding timeout')
        with self.assertRaises(Conflict): self.executor.resume(self.space, job['id'])
        with patch('research_agent.experiments.codex_path', return_value='codex.exe'):
            fresh = self.executor.retry(self.space, job['id'])
        self.assertNotEqual(fresh['id'], job['id'])

    def test_resume_completed_code_uses_same_job_and_new_generation(self):
        run = self.submit(); job = self.store.claim_next()
        self.executor.save(job, {'phase': 'evaluating', 'coding_invocations': 1, 'code_hash': 'frozen', 'evaluation_attempts': 2})
        self.store.finish(job['id'], 'failed', 'evaluation interrupted')
        resumed = self.executor.resume(self.space, job['id'])
        self.assertEqual(resumed['id'], job['id']); self.assertEqual(resumed['execution_generation'], 1)

    def test_command_contract_disables_global_rules_and_cleans_credentials(self):
        with patch('research_agent.experiment_process.codex_path', return_value='codex.exe'):
            command = coding_command(self.root)
        self.assertIn('--ignore-user-config', command); self.assertIn('--ignore-rules', command)
        self.assertNotIn('danger-full-access', ' '.join(command))
        policy = permissions(self.root)
        self.assertFalse(policy['network']['enabled'])
        from research_agent.experiment_process import ROOT
        self.assertEqual(policy['filesystem'][str((ROOT / 'docs').resolve())], 'deny')
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'private', 'SUDOCODE_API_KEY': 'private'}):
            self.assertNotIn('OPENAI_API_KEY', clean_environment()); self.assertNotIn('SUDOCODE_API_KEY', clean_environment())

    def test_deleted_job_at_dispatch_does_not_escape_worker(self):
        run=self.submit();job=self.store.claim_next()
        self.store.cancel(self.space,job['id']);self.store.delete_space(self.space)
        self.executor.execute(job)
        other=self.store.save_space({'name':'still running'})['id']
        self.assertEqual(self.store.space(other)['name'],'still running')

    def test_resume_cannot_duplicate_plan_in_another_conversation(self):
        run=self.submit();job=self.store.claim_next()
        self.executor.save(job,{'phase':'evaluating','coding_invocations':1,'code_hash':'x','evaluation_attempts':2})
        self.store.finish(job['id'],'failed','interrupted')
        self.body['conversation_id']=self.store.create_conversation(self.space,'other')['id']
        self.submit()
        with self.assertRaises(Conflict):self.app.resume(self.space,job['id'])
        with self.assertRaises(Conflict):self.app.sessions.resume(self.space,job['id'])

    def test_invalid_prepare_does_not_leave_orphan_baseline(self):
        count=len(self.app.records.experiments(self.space))
        for body in ({'hypothesis':'x'*2000},{'hypothesis':'x','idea_id':'missing'}):
            with self.assertRaises((ValueError,NotFound)):self.executor.prepare(self.space,body)
        self.assertEqual(len(self.app.records.experiments(self.space)),count)

    def test_completed_receipt_is_required_for_postprocessing_recovery(self):
        output=self.root/'receipt';output.mkdir()
        (output/'codex.json').write_text(json.dumps({'exit_code':0,'termination':'completed','candidate_hash':'frozen','stdout':'malformed redacted legacy output'}))
        (output/'events.jsonl').write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}})+'\n')
        self.assertTrue(self.executor.coding_receipt(output))
        (output/'events.jsonl').write_text(json.dumps({'type':'error','message':'Reconnecting...'})+'\n'+json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}})+'\n')
        self.assertTrue(self.executor.coding_receipt(output))
        (output/'codex.json').write_text(json.dumps({'exit_code':0,'termination':'timeout'}))
        self.assertIsNone(self.executor.coding_receipt(output))

    def test_http_remote_execution_retry_and_resume_are_all_denied(self):
        import threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from server import make_server,Handler
        server=make_server(port=0,db_path=self.store.path,trace_dir=self.root/'http-traces',model_factory=OfflineRouter,start_worker=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,3);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        run=self.submit();prefix=f'http://127.0.0.1:{server.server_port}/api/spaces/{self.space}'
        output=self.executor.root/run['job_id'];output.mkdir(parents=True)
        (output/'dev-feedback.json').write_text('{"rows":[]}',encoding='utf-8')
        artifact_url=prefix+'/research-records/executions/'+run['job_id']+'/artifacts/'
        with urlopen(artifact_url+'dev-feedback.json',timeout=3) as response:
            self.assertEqual(json.load(response),{'rows':[]})
        with self.assertRaises(HTTPError) as denied:urlopen(artifact_url+'private-file.json',timeout=3)
        self.assertEqual(denied.exception.code,404)
        paths=[f'/research-records/experiment-versions/{self.version}/execute',f'/jobs/{run["job_id"]}/resume',f'/jobs/{run["job_id"]}/retry']
        with patch.object(Handler,'local_desktop',return_value=False):
            for path in paths:
                with self.assertRaises(HTTPError) as exc:urlopen(Request(prefix+path,data=json.dumps(self.body).encode(),headers={'Content-Type':'application/json'}),timeout=3)
                self.assertEqual(exc.exception.code,403)

    def test_evaluation_resume_reuses_completed_coding_without_model_replay(self):
        run=self.submit();job=self.store.claim_next();calls=[]
        result={'valid':True,'metrics':{'recall_at5':30.,'mrr':.2,'latency_ms':2.},'process':{'termination':'completed'}}
        def coding(args,workspace,**kwargs):
            calls.append('coding')
            (workspace/'ranker.py').write_text(BASELINE+'\n# candidate\n',encoding='utf-8')
            event={'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':2}}
            kwargs['event'](event)
            return {'exit_code':0,'termination':'completed','seconds':1,'tool_calls':1,'stdout':json.dumps(event)+'\n','stderr':''}
        with patch.dict('os.environ',{'SUDOCODE_API_KEY':'test-placeholder'}), patch('research_agent.experiment_process.codex_path',return_value='codex.exe'), patch('research_agent.experiments.run_process',side_effect=coding), patch('research_agent.experiments.evaluate',side_effect=[result,{'valid':False,'error':'nonzero_exit','process':{'exit_code':3}}]):
            self.executor.execute(job)
        failed=self.executor.get(self.space,job['id'])
        self.assertEqual(failed['job']['status'],'failed');self.assertNotIn('candidate',failed['state'])
        self.app.resume(self.space,job['id'])
        with patch('research_agent.experiments.run_process',side_effect=AssertionError('must not re-run Codex')),patch('research_agent.experiments.evaluate',return_value=result):
            self.executor.execute(self.store.claim_next())
        final=self.executor.get(self.space,job['id']);self.assertEqual(final['job']['status'],'completed')
        self.assertEqual(calls,['coding']);self.assertEqual(final['state']['evaluation_attempts'],3)
        self.assertEqual(self.app.sessions.context(self.space,self.chat)['coding_usage']['total_tokens'],14)

    def test_self_modifying_candidate_is_not_cached_for_resume(self):
        run=self.submit();job=self.store.claim_next()
        result={'valid':True,'metrics':{'recall_at5':30.,'mrr':.2,'latency_ms':2.}}
        def coding(args,workspace,**kwargs):
            event={'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':1}};kwargs['event'](event)
            return {'exit_code':0,'termination':'completed','seconds':1,'tool_calls':0,'stdout':json.dumps(event)+'\n','stderr':''}
        count=[]
        def measured(workspace,*args):
            count.append(1)
            if len(count)==2:(workspace/'ranker.py').write_text(BASELINE+'\n# changed during evaluation',encoding='utf-8')
            return result
        with patch.dict('os.environ',{'SUDOCODE_API_KEY':'test-placeholder'}),patch('research_agent.experiment_process.codex_path',return_value='codex.exe'),patch('research_agent.experiments.run_process',side_effect=coding),patch('research_agent.experiments.evaluate',side_effect=measured):
            self.executor.execute(job)
        failed=self.executor.get(self.space,job['id'])
        self.assertEqual(failed['job']['status'],'failed');self.assertNotIn('candidate',failed['state'])
        self.assertIsNone(failed['result_version_id'])


    def test_cancelled_coding_retains_process_receipt_without_recovery_authorization(self):
        self.process_receipt_failure(cancel=True)

    def test_contract_violation_retains_process_receipt_without_recovery_authorization(self):
        self.process_receipt_failure(cancel=False)

    def process_receipt_failure(self, *, cancel):
        run=self.submit();job=self.store.claim_next()
        baseline={'valid':True,'metrics':{'recall_at5':30.,'mrr':.2,'latency_ms':2.}}
        def coding(args,workspace,**kwargs):
            event={'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':2}}
            kwargs['event'](event)
            if cancel:self.store.cancel(self.space,job['id'])
            else:(workspace/'unexpected.txt').write_text('contract violation',encoding='utf-8')
            return {'exit_code':-1 if cancel else 0,'termination':'cancelled' if cancel else 'completed',
                    'seconds':7.5,'tool_calls':1,'stdout':json.dumps(event)+'\n','stderr':'test-placeholder process diagnostic'}
        with patch.dict('os.environ',{'SUDOCODE_API_KEY':'test-placeholder'}),patch('research_agent.experiment_process.codex_path',return_value='codex.exe'),patch('research_agent.experiments.run_process',side_effect=coding),patch('research_agent.experiments.evaluate',return_value=baseline):
            self.executor.execute(job)
        result=self.executor.get(self.space,job['id'])
        receipt=json.loads(self.executor.artifact(self.space,job['id'],'codex.json').read_text(encoding='utf-8'))
        self.assertEqual(result['job']['status'],'cancelled' if cancel else 'failed')
        self.assertEqual(receipt['termination'],'cancelled' if cancel else 'completed')
        self.assertEqual(receipt['exit_code'],-1 if cancel else 0)
        self.assertEqual(receipt['seconds'],7.5)
        self.assertIn('turn.completed',receipt['stdout'])
        self.assertIn('process diagnostic',receipt['stderr']);self.assertNotIn('test-placeholder',receipt['stderr'])
        self.assertNotIn('candidate_hash',receipt)
        self.assertIsNone(self.executor.coding_receipt(self.executor.root/job['id']))
        self.assertIsNone(result['result_version_id'])

    def test_evidence_usage_survives_measurement_failure_and_cached_resume(self):
        from research_agent import experiment_evidence as evidence, experiment_feedback as feedback
        from research_agent.usage import summarize_usage
        corpus=[dict(id='paper:0',title='Study',section='method',content='alpha',url='https://example.org/study',page=1)]
        questions=[dict(id=split,category='direct',split=split,query=split+' alpha',gold=[dict(document='paper',ordinal=0)])
                   for split in ('dev','held_out')]
        fixture={'corpus':corpus,'questions':questions,'signals':[{'text':q['query'],'dense':[]} for q in questions],
                 'features':{'signals_hash':'fixture'}}
        usage={'input_tokens':7,'output_tokens':3,'total_tokens':10}
        preparations=[]
        def prepare(data,cancelled):
            hit=bool(preparations);preparations.append(hit)
            result=copy.deepcopy(fixture)
            result['evidence_records']=[{'question_id':q['id'],'cache_key':q['id'],'cache_hit':hit,'usage_records':[usage]} for q in questions]
            result['features']['evidence_rerank']={'current_usage':summarize_usage([] if hit else [usage,usage]),
                                                  'cache_hits':len(questions) if hit else 0,'preparation_seconds':1.}
            return result
        measured={'valid':True,'metrics':{'recall_at5':100.,'mrr':1.},'validation':{'recall_at5':100.,'mrr':1.},
                  'rows':feedback.score(corpus,questions,[['paper:0'],['paper:0']])}
        config=feedback.configuration();config.update(dataset=evidence.SUITE,command='registered:'+evidence.SUITE)
        def coding(args,workspace,**kwargs):
            event={'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':2}};kwargs['event'](event)
            return {'exit_code':0,'termination':'completed','seconds':1,'tool_calls':0,'stdout':json.dumps(event)+'\n','stderr':''}
        with patch.object(evidence,'configuration',return_value=config),patch.object(evidence,'frozen_data',return_value=(fixture,'fixture')),patch.object(evidence,'prepare_data',side_effect=prepare),patch.object(evidence,'evaluate',side_effect=[measured,{'valid':False,'error':'nonzero_exit'},measured]),patch.dict('os.environ',{'SUDOCODE_API_KEY':'test-placeholder'}),patch('research_agent.experiment_process.codex_path',return_value='codex.exe'),patch('research_agent.experiments.codex_path',return_value='codex.exe'),patch('research_agent.experiments.run_process',side_effect=coding) as coding_call:
            plan=self.executor.prepare(self.space,{'hypothesis':'fixture evidence','suite':evidence.SUITE})
            run=self.executor.submit(self.space,plan['versions'][0]['id'],self.body)
            self.executor.execute(self.store.claim_next())
            self.assertEqual(self.executor.get(self.space,run['job_id'])['job']['status'],'failed')
            self.app.resume(self.space,run['job_id']);self.executor.execute(self.store.claim_next())
            self.assertEqual(self.executor.get(self.space,run['job_id'])['job']['status'],'completed')
            self.assertEqual(coding_call.call_count,1)
        receipt=json.loads(self.executor.artifact(self.space,run['job_id'],'semantic-signals.json').read_text(encoding='utf-8'))
        stats=receipt['features']['evidence_rerank']
        self.assertEqual(preparations,[False,True])
        self.assertEqual(stats['current_usage']['requests'],2)
        self.assertEqual(stats['current_usage']['total_tokens'],20)
        self.assertEqual(stats['latest_preparation_usage']['requests'],0)
        self.assertEqual(stats['cache_hits'],2)
        self.assertEqual([entry['cache_hits'] for entry in stats['preparation_history']],[0,2])
        self.assertEqual({r['cache_key'] for r in stats['run_usage_records']},{'dev','held_out'})


if __name__ == '__main__': unittest.main()
