"""Feedback contracts: real local fixture execution, no external models."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_agent import experiment_feedback as f
from research_agent.experiments import Experiments
from research_agent.workbench import Workbench, OfflineRouter
from research_agent.workbench_store import WorkbenchStore


def fixture():
    corpus=[dict(id='paper:0',title='Study',section='method',content='alpha retrieval',url='https://example.org/study',page=1),
            dict(id='paper:1',title='Study',section='results',content='beta reasoning',url='https://example.org/study',page=2)]
    questions=[dict(id='dev-example',category='direct',split='dev',query='alpha 中文诊断',gold=[dict(document='paper',ordinal=0)]),
               dict(id='private-test-example',category='semantic',split='held_out',query='secret held-out wording',gold=[dict(document='paper',ordinal=1)])]
    signals=[dict(text=q['query'],dense=[dict(id='paper:'+str(i),score=.9)]) for i,q in enumerate(questions)]
    return dict(corpus=corpus,questions=questions,signals=signals,features={'signals_hash':'frozen-fixture'})


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.data=fixture()
        (self.root/'ranker.py').write_text(f.BASELINE,encoding='utf-8')

    def runner(self,*args,**kwargs):
        result=subprocess.run([sys.executable,'-I','-B','-c',f.PREDICT],cwd=args[1],input=kwargs['stdin'],capture_output=True,text=True,encoding='utf-8',timeout=10)
        return dict(stdout=result.stdout,stderr=result.stderr,termination='completed' if result.returncode==0 else 'nonzero_exit',exit_code=result.returncode,seconds=1.)

    def measured(self,workspace,data,*args):
        with patch.object(f,'sandbox_command',return_value=['fixture']):
            return f.evaluate(workspace,data,lambda:False,runner=self.runner)

    def test_host_selector_keeps_baseline_on_development_regression(self):
        baseline = {'validation': {'recall_at5': 90.0, 'mrr': .70}}
        base = f.development_snapshot('baseline', baseline, 'base-hash')
        decision = f.select_best_development(base, [
            {'label': 'candidate-1', 'code_hash': 'bad', 'metrics': {'recall_at5': 95.0, 'mrr': .69}},
            {'label': 'candidate-2', 'code_hash': 'good', 'metrics': {'recall_at5': 91.0, 'mrr': .70}},
        ])
        self.assertEqual(decision['selected'], 'candidate-2')
        self.assertEqual([row['label'] for row in decision['rejected']], ['candidate-1'])
        # A candidate that improves Recall but harms MRR can never be selected.
        self.assertEqual(decision['baseline'], {'recall_at5': 90.0, 'mrr': .70})

    def test_host_selector_falls_back_to_baseline_when_all_trials_regress(self):
        base = f.development_snapshot('baseline', {'validation': {'recall_at5': 90.0, 'mrr': .70}}, 'base')
        decision = f.select_best_development(base, [
            {'label': 'candidate-1', 'metrics': {'recall_at5': 80.0, 'mrr': .71}},
            {'label': 'candidate-2', 'metrics': {'recall_at5': 90.0, 'mrr': .69}},
        ])
        self.assertEqual(decision['selected'], 'baseline')
        self.assertEqual(len(decision['rejected']), 2)

    def test_unanswerable_development_feedback_keeps_quality_metrics_empty(self):
        data=copy.deepcopy(self.data)
        data['questions'][0]['gold']=[]
        rows=f.score(data['corpus'], data['questions'], [[], ['paper:1']])
        files=f.development_files(data, {'rows': rows})
        self.assertEqual(files['dev-feedback.json']['summary'],
                         {'recall_at5': None, 'mrr': None, 'zero_recall': 0, 'answerable': 0})
        self.assertEqual(files['dev-feedback.json']['rows'][0]['expected_evidence'], [])
        self.assertIsNone(f.summarize([rows[0]])['recall_at5'])

    def test_local_development_tool_matches_host_scoring_without_test_labels(self):
        baseline=self.measured(self.root,self.data)
        self.assertTrue(baseline['valid'])
        files=f.development_files(self.data,baseline)
        self.assertNotIn('private-test-example',json.dumps(files))
        self.assertNotIn('secret held-out wording',json.dumps(files))
        self.assertEqual(files['dev-feedback.json']['rows'][0]['expected_evidence'][0]['page'],1)
        self.assertEqual(files['dev-feedback.json']['rows'][0]['dense_gold_ranks'],{'paper:0':1})
        for name,value in files.items():f.dump(self.root/name,value)
        f.dump(self.root/'corpus.json',self.data['corpus'])
        f.dump(self.root/'dev.json',[self.data['questions'][0]])
        for name in ('candidate-first.json', 'candidate-revised.json'): f.dump(self.root/name, {})
        (self.root/'dev-eval.py').write_text(f.DEV_EVAL,encoding='utf-8')
        result=subprocess.run([sys.executable,'-X','utf8=0','-I','-B','dev-eval.py'],cwd=self.root,capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['summary'],baseline['validation'])
        self.assertEqual(set(baseline['metrics']),{'recall_at5','mrr'})
        self.assertGreaterEqual(baseline['timing']['rank_ms_per_query'],0)
        self.assertEqual(baseline['timing']['process_wall_ms'],1000)

    def test_native_python_stdin_preserves_unicode_in_both_evaluators(self):
        from research_agent import experiment_retrieval as old
        data=fixture();data['corpus'][0]['content']='中文原文 • evidence'
        code="def rank(corpus, queries, top_k=10):\n    assert corpus[0]['content']=='中文原文 • evidence',repr(corpus[0]['content'])\n    q=queries[0]['text'] if isinstance(queries[0],dict) else queries[0]\n    assert q=='alpha 中文诊断',repr(q)\n    return [['paper:0'],['paper:1']]\n"
        (self.root/'ranker.py').write_text(code,encoding='utf-8')
        def native(args,workspace,**kwargs):
            run=subprocess.run(args,cwd=workspace,input=kwargs['stdin'].encode('utf-8'),capture_output=True,timeout=10)
            return dict(stdout=run.stdout.decode('utf-8',errors='replace'),stderr=run.stderr.decode('utf-8',errors='replace'),termination='completed' if run.returncode==0 else 'nonzero_exit',exit_code=run.returncode,seconds=1.)
        for module in (old,f):
            with patch.object(module,'sandbox_command',side_effect=lambda workspace,command,*a:command):
                result=module.evaluate(self.root,data,lambda:False,runner=native)
            self.assertTrue(result['valid'],(module.SUITE,result))

    def test_metrics_are_computed_from_ids_and_unstable_or_invalid_output_is_rejected(self):
        def measure(value):
            with patch.object(f,'sandbox_command',return_value=['fixture']):
                return f.evaluate(self.root,self.data,lambda:False,runner=lambda *a,**kw:dict(stdout=json.dumps(value),termination='completed',exit_code=0,seconds=1))
        correct=[['paper:0'],['paper:1']]
        self.assertEqual(measure(dict(predictions=[correct]*3,rank_batch_ms=[1,2,3],recall_at5=-100))['metrics']['recall_at5'],100)
        for value in [dict(recall_at5=100),dict(predictions=[correct,correct,[[],[]]],rank_batch_ms=[1,2,3]),
                      dict(predictions=[correct]*3,rank_batch_ms=[-1,2,3]),
                      dict(predictions=[[['unknown'],['paper:1']]]*3,rank_batch_ms=[1,2,3])]:
            self.assertFalse(measure(value)['valid'])

    def test_mutable_rank_return_is_copied_before_repeatability_check(self):
        # A candidate can accidentally return one shared list and mutate it on
        # the next call. The evaluator must see the three actual snapshots.
        code = """
state = [['paper:0'], ['paper:1']]
calls = 0
def rank(corpus, queries, top_k=10):
    global calls
    calls += 1
    state[0] = ['paper:0'] if calls % 2 else []
    return state
"""
        (self.root/'ranker.py').write_text(code, encoding='utf-8')
        result = self.measured(self.root, self.data)
        self.assertFalse(result['valid'])
        self.assertIn('重复测量输出不一致', result['error'])

    def test_model_identity_changes_when_local_encoder_installation_changes(self):
        first = self.root / 'model-a'; first.mkdir()
        (first/'config.json').write_text('{\\\"hidden_size\\\":1}', encoding='utf-8')
        (first/'researchagent-revision.txt').write_text(f.REVISION, encoding='utf-8')
        (first/'weights.bin').write_bytes(b'a')
        second = self.root / 'model-b'; second.mkdir()
        (second/'config.json').write_text('{\\\"hidden_size\\\":1}', encoding='utf-8')
        (second/'researchagent-revision.txt').write_text(f.REVISION, encoding='utf-8')
        (second/'weights.bin').write_bytes(b'a')
        self.assertNotEqual(f.model_identity(first)['fingerprint'], f.model_identity(second)['fingerprint'])
        (second/'weights.bin').write_bytes(b'changed-weight')
        self.assertNotEqual(f.model_identity(first)['fingerprint'], f.model_identity(second)['fingerprint'])

    def test_default_feedback_run_preserves_legacy_suite_and_finalizes_host_results(self):
        self.loop_test()

    def test_host_keeps_first_candidate_when_revision_regresses_before_final_test(self):
        base = "def rank(corpus,queries,top_k=10):\n    return [[] for q in queries]\n"
        first = "def rank(corpus,queries,top_k=10):\n    return [[corpus[0]['id']] for q in queries]\n"
        revised = base + "# losing revision\n"
        with patch.object(f, 'BASELINE', base):
            result, executor, space, run = self.loop_test(candidates=[first, revised])
        self.assertEqual(result['state']['selected_label'], 'candidate-1')
        self.assertEqual(executor.artifact(space,run['job_id'],'ranker.py').read_text(encoding='utf-8'), first)
        self.assertEqual(self.host_splits, [['dev','held_out'], ['dev'], ['dev'], ['dev','held_out']])
        saved=json.loads(executor.artifact(space,run['job_id'],'development-snapshots.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['trials'][1]['code'], revised)
        self.assertEqual(saved['trials'][1]['metrics']['recall_at5'], 0)
        self.assertEqual(saved['trials'][0]['metrics']['recall_at5'], 100)
        result_artifact=json.loads(executor.artifact(space,run['job_id'],'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result_artifact['selected_code_hash'], f.sha(first))
        self.assertEqual(result_artifact['comparison']['right']['content']['config']['code_revision'], f.sha(first))
        self.assertEqual(result_artifact['comparison']['right']['content']['results'], result['state']['candidate']['metrics'])

    def test_host_reverts_to_baseline_and_retains_all_negative_candidates(self):
        bad = "def rank(corpus,queries,top_k=10):\n    return [[] for q in queries]\n"
        result, executor, space, run = self.loop_test(candidates=[bad, bad+"# second failure\n"])
        self.assertEqual(result['state']['selected_label'], 'baseline')
        self.assertEqual(executor.artifact(space,run['job_id'],'ranker.py').read_text(encoding='utf-8'), f.BASELINE)
        saved=json.loads(executor.artifact(space,run['job_id'],'development-snapshots.json').read_text(encoding='utf-8'))
        self.assertEqual(len(saved['decision']['rejected']), 2)
        self.assertEqual(result['state']['candidate']['metrics']['recall_at5'], 100)
        self.assertEqual(self.host_splits, [['dev','held_out'], ['dev'], ['dev'], ['dev','held_out']])

    def test_invalid_candidate_is_kept_as_negative_and_baseline_still_finishes(self):
        invalid = "def rank(:\n"
        result, executor, space, run = self.loop_test(candidates=[invalid],allow_dev_failure=True)
        self.assertEqual(result['state']['selected_label'], 'baseline')
        self.assertEqual(result['state']['development_evaluation_attempts'], 1)
        self.assertFalse(result['state']['development']['trials'][0]['measurement']['valid'])
        self.assertEqual(result['state']['development']['trials'][0]['code'], invalid)

    def test_restart_during_development_restores_code_without_replaying_reserved_attempt(self):
        first = "def rank(corpus,queries,top_k=10):\n    return [[corpus[0]['id']] for q in queries]\n"
        revised = "def rank(corpus,queries,top_k=10):\n    return [[] for q in queries]\n"
        result, executor, space, run = self.loop_test(candidates=[first,revised],interrupt_development=True)
        self.assertEqual(result['state']['selected_label'], 'baseline')
        self.assertEqual(result['state']['development_evaluation_attempts'], 2)
        self.assertEqual(result['state']['development']['trials'][0]['measurement_status'], 'interrupted')
        self.assertEqual(result['state']['development']['trials'][0]['metrics'], {})
        self.assertEqual(result['state']['coding_invocations'], 1)
        self.assertEqual(executor.artifact(space,run['job_id'],'ranker.py').read_text(encoding='utf-8'), f.BASELINE)

    def test_candidate_slot_injection_after_code_checkpoint_cannot_change_selected_code(self):
        base = "def rank(corpus,queries,top_k=10):\n    return [[] for q in queries]\n"
        with patch.object(f, 'BASELINE', base):
            result, executor, space, run = self.loop_test(candidates=[base+"# negative candidate\n"],checkpoint_tamper=True)
        self.assertEqual(result['job']['status'], 'failed')
        self.assertIn('编码完成后开发候选快照被修改', result['state']['failure']['error'])
        self.assertEqual(result['state']['development']['snapshots'][0]['code'], base+"# negative candidate\n")
        self.assertEqual(self.host_splits, [['dev','held_out']])
        self.assertNotIn('candidate', result['state'])
        self.assertIsNone(result['result_version_id'])
        receipt=json.loads(executor.artifact(space,run['job_id'],'codex.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['candidate_slot_hashes'], result['state']['development']['slot_hashes'])

    def test_receipt_resume_rejects_changed_candidate_slot_before_postprocessing(self):
        bad = "def rank(corpus,queries,top_k=10):\n    return [[] for q in queries]\n"
        result, executor, space, run = self.loop_test(candidates=[bad],receipt_tamper=True)
        self.assertEqual(result['job']['status'], 'failed')
        self.assertIn('编码完成后开发候选快照被修改', result['state']['failure']['error'])
        self.assertEqual(self.host_splits, [['dev','held_out']])
        self.assertNotIn('code_hash', result['state'])
        self.assertIsNone(result['result_version_id'])

    def test_candidate_slot_hard_link_is_rejected_before_reading(self):
        target=self.root/'private-source.json';target.write_text('not a candidate',encoding='utf-8')
        slot=self.root/'candidate-first.json';os.link(target,slot)
        with self.assertRaisesRegex(ValueError,'不能包含链接'):
            Experiments.candidate_slot_bytes(self.root,'candidate-first.json')

    def test_candidate_cannot_modify_development_material_during_measurement(self):
        self.loop_test(tamper=True)

    def loop_test(self,tamper=False,candidates=None,allow_dev_failure=False,interrupt_development=False,checkpoint_tamper=False,receipt_tamper=False):
        store=WorkbenchStore(self.root/'state.db');app=Workbench(store,OfflineRouter,lambda *a:None,self.root/'traces',start_worker=False);self.addCleanup(app.close)
        executor=app.experiments;executor.work_root=self.root/'workspaces'
        space=store.save_space({'name':'feedback'})['id'];chat=store.create_conversation(space,'independent')['id']
        plan=executor.prepare(space,{'hypothesis':'Inspect actual development failures'})
        self.assertEqual(plan['versions'][0]['content']['config']['dataset'],f.SUITE)
        legacy=executor.prepare(space,{'hypothesis':'legacy','suite':'public-retrieval-v1'})
        self.assertEqual(legacy['versions'][0]['content']['config']['dataset'],'public-retrieval-v1')
        with patch('research_agent.experiments.codex_path',return_value='codex'):
            run=executor.submit(space,plan['versions'][0]['id'],dict(conversation_id=chat,authorize_execution=True,seconds=60,token_budget=2000))
        original_save=executor.save
        original_dump=f.dump
        injected=[]
        workspace=Path(run['contract']['workspace'])
        def inject_slot():
            code="def rank(corpus,queries,top_k=10):\n    return [[corpus[0]['id']] for q in queries]\n"
            f.dump(workspace/'candidate-first.json',{'code':code,'code_hash':f.sha(code)})
        def saved(job,state):
            original_save(job,state)
            if checkpoint_tamper and state.get('code_hash') and not state.get('development_selected') and not injected:
                self.assertIn('snapshots',state['development'])
                injected.append(True);inject_slot()
        def dumped(path,value):
            original_dump(path,value)
            if receipt_tamper and Path(path).name=='codex.json' and value.get('candidate_hash') and not injected:
                injected.append(True);inject_slot()
                raise RuntimeError('simulated restart after coding receipt')
        original_evaluate=f.evaluate
        self.host_splits=[]
        def measured(workspace,data,*args):
            self.host_splits.append([q['split'] for q in data['questions']])
            if interrupt_development and len(self.host_splits)==2: raise RuntimeError('simulated dev restart')
            with patch.object(f,'sandbox_command',return_value=['fixture']):
                return original_evaluate(workspace,data,lambda:False,runner=self.runner)
        def coding(args,workspace,**kwargs):
            visible=''.join((workspace/name).read_text(encoding='utf-8') for name in ['README.md','dev.json','dev-inputs.json','dev-feedback.json'])
            self.assertNotIn('private-test-example',visible);self.assertNotIn('secret held-out wording',visible)
            (workspace/'proposal.md').write_text('HYPOTHESIS. Development evidence inspected; test performance unknown.',encoding='utf-8')
            for candidate in candidates or []:
                (workspace/'ranker.py').write_text(candidate,encoding='utf-8')
                check=subprocess.run([sys.executable,'-X','utf8','-I','-B','dev-eval.py'],cwd=workspace,capture_output=True,text=True,encoding='utf-8',timeout=10)
                if not allow_dev_failure: self.assertEqual(check.returncode,0,check.stderr)
            if tamper:
                (workspace/'ranker.py').write_text(f.BASELINE+"\nold_rank=rank\ndef rank(*args,**kwargs):\n    from pathlib import Path\n    Path('dev.json').write_text('[]')\n    return old_rank(*args,**kwargs)\n",encoding='utf-8')
            event={'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':3}};kwargs['event'](event)
            return dict(exit_code=0,termination='completed',seconds=1,stdout=json.dumps(event)+'\n',stderr='')
        frozen_configuration=f.configuration()
        with patch.object(f,'configuration',return_value=frozen_configuration),patch('research_agent.experiments.frozen_data',return_value=(self.data,'fixture')),patch.object(f,'frozen_data',return_value=(self.data,'fixture')),patch.object(f,'prepare_data',side_effect=lambda data,cancelled:data),patch.object(f,'evaluate',side_effect=measured),patch('research_agent.experiments.run_process',side_effect=coding),patch('research_agent.experiment_process.codex_path',return_value='codex'),patch.dict('os.environ',{'SUDOCODE_API_KEY':'test-placeholder'}),patch.object(executor,'save',side_effect=saved),patch('research_agent.experiments.dump',side_effect=dumped):
            executor.execute(store.claim_next())
            if receipt_tamper:
                self.assertEqual(executor.get(space,run['job_id'])['job']['status'],'failed')
                app.resume(space,run['job_id'])
                with patch('research_agent.experiments.run_process',side_effect=AssertionError('must not replay coding')):
                    executor.execute(store.claim_next())
            if interrupt_development:
                failed=executor.get(space,run['job_id'])
                self.assertEqual(failed['job']['status'],'failed')
                self.assertEqual(failed['state']['development']['trials'][0]['measurement_status'],'started')
                app.resume(space,run['job_id'])
                with patch('research_agent.experiments.run_process',side_effect=AssertionError('must not replay coding')):
                    executor.execute(store.claim_next())
        result=executor.get(space,run['job_id'])
        if receipt_tamper or checkpoint_tamper:
            self.assertEqual(result['job']['status'],'failed')
        elif tamper:
            self.assertEqual(result['state']['coding_invocations'],1)
            self.assertIn('候选执行期间修改了工作目录',result['state']['failure']['error'])
            self.assertEqual(result['job']['status'],'failed');self.assertNotIn('candidate',result['state']);self.assertIsNone(result['result_version_id'])
        else:
            self.assertEqual(result['job']['status'],'completed',result['state'])
            self.assertEqual(result['state']['coding_invocations'],1);self.assertEqual(result['state']['evaluation_attempts'],2)
            if candidates is None: self.assertEqual(result['state']['candidate']['metrics']['recall_at5'],100)
            self.assertTrue(executor.artifact(space,run['job_id'],'proposal.md').is_file())
        return result,executor,space,run

if __name__=='__main__':unittest.main()
