"""Opt-in real Windows test for hidden labels and host-scored predictions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from uuid import uuid4
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(os.name == 'nt' and os.environ.get('RESEARCH_RUN_NATIVE_TESTS') == '1', 'native sandbox opt-in')
class LoCoMoNativeTests(unittest.TestCase):
    def test_current_workspace_is_writable_but_labels_raw_data_and_siblings_are_denied(self):
        with tempfile.TemporaryDirectory(prefix='locomo-native-') as folder:
            folder = Path(folder)
            private = folder/'host'; private.mkdir()
            store = WorkbenchStore(private/'state.sqlite')
            app = Workbench(store, lambda: SimpleNamespace(name='offline-fixture'), None, private/'traces', start_worker=False)
            app.coding.work_root = ROOT/'experiments'/('locomo-native-work-'+uuid4().hex)
            try:
                tasks = [{'id':'q','conversation_id':'c','split':'development','category':1}]
                corpus = [{'conversation_id':'c','split':'development','conversation':{'session_1':[{'dia_id':'D1:1'}]}}]
                labels = [{'id':'q','answer':'host secret','gold_evidence':['D1:1'],'retrieval_scorable':True}]
                raw = {name:json.dumps(value).encode() for name,value in [('tasks',tasks),('corpus',corpus),('labels',labels)]}
                contract = {'name':'locomo_qa_v1','seeds':[13],'tasks_path':'tasks.json','corpus_path':'corpus.json',
                            **{name+'_sha256':hashlib.sha256(value).hexdigest() for name,value in raw.items()}}
                space=store.save_space({'name':'native private scoring'})['id']
                chat=store.create_conversation(space,'native scoring')['id']
                app.auto_research.enqueue(space,chat,'Host scoring isolation',metric_contract=contract,
                                          budget={'coding_calls':0,'experiment_seconds':60})
                job=store.claim_next(); workspace=app.coding.work_root/job['id'];workspace.mkdir(parents=True)
                host=private/'auto-research'/job['id'];host.mkdir(parents=True)
                (host/'scoring-labels.json').write_bytes(raw['labels'])
                for name in ('tasks','corpus'):(workspace/(name+'.json')).write_bytes(raw[name])
                sibling=app.coding.work_root/'other';sibling.mkdir();(sibling/'labels.json').write_text('private sibling')
                denied=[host/'scoring-labels.json', sibling/'labels.json', ROOT/'datasets/open/locomo/labels.json',
                        Path('D:/paper/researchagent-benchmarks/raw/locomo/locomo-3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376.zip'),
                        Path('D:/paper/researchagent-sources/AgenticMemory/0c8039f28fdcc08189a23c07a3437d9d2482f9c2/data/locomo10.json')]
                self.assertTrue(all(p.is_file() for p in denied))
                script = '''import json,os
from pathlib import Path
assert os.environ.get('PYTHON_DOTENV_DISABLED')=='1'
paths=DENIED_PATHS
for path in paths:
    try: Path(path).read_bytes()
    except (PermissionError,FileNotFoundError): pass
    else: raise AssertionError('Private path was readable')
Path('prediction.jsonl').write_text(json.dumps({'task_id':'q','seed':13,'answer':'wrong','evidence_ids':['D1:1']}))
Path('result.json').write_text(json.dumps({'metrics':{'answer_f1':1,'latency_ms':0},'config':{'dataset':'locomo','dataset_version':TASK_SHA,'split':'development','seeds':[13]},'diagnostics':{'predictions_path':'prediction.jsonl'}}))
print('private paths denied; current workspace writable')
'''.replace('DENIED_PATHS',repr([str(p) for p in denied])).replace('TASK_SHA',repr(contract['tasks_sha256']))
                (workspace/'evaluate.py').write_text(script,encoding='utf-8')
                task=app.coding.submit(job,'native-locomo',{'task':'Check host label isolation','plan':'Fixture only, no model-quality claim',
                    'execution_only':True,'seconds':0,'token_budget':0,
                    'commands':[{'name':'native-scoring','role':'diagnostic','script':'evaluate.py','args':[],'result_path':'result.json','seconds':60}]})
                app.coding.execute(space,task['id'])
                task=app.coding.get(space,task['id'])
                output=ROOT/'evals/reports/amem-environment-20260930/native-label-boundary'/task['id']
                output.mkdir(parents=True,exist_ok=True)
                (output/'task.json').write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding='utf-8')
                for file in (app.coding.output_root/task['id']).iterdir():
                    if file.is_file():shutil.copyfile(file,output/file.name)
                self.assertEqual(task['status'],'completed',task['state'].get('error'))
                measurement=task['state']['measurements'][0]
                self.assertTrue(measurement['valid'],measurement)
                self.assertEqual(measurement['metrics']['answer_f1'],0)
                self.assertNotIn('latency_ms',measurement['metrics'])
            finally:
                app.close()