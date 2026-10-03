"""Isolated V2 acceptance. --live fetches real documents and uses the selected configured model."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research_agent import load_dotenv, model_from_env
from research_agent.library import model_json
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore
from research_agent.trace import redact, now_iso
from tests.test_v2 import GroundedModel, pdf_bytes
from research_agent.contracts import ModelDecision


class LocalWorkflowModel(GroundedModel):
    """Deterministic current retrieve/read_evidence contract, not the retired Library.answer path."""
    def complete(self,messages,tools):
        if not any(t['function']['name']=='retrieve' for t in tools):
            return super().complete(messages,tools)
        data=[json.loads(m['content'])['UNTRUSTED_TOOL_DATA'] for m in messages if m['role']=='tool']
        reads=[d for d in data if d.get('kind')=='read_evidence']
        if data and data[-1]['kind']=='retrieve':
            return ModelDecision('tool_call',tool_name='read_evidence',arguments={'ref_id':data[-1]['results'][0]['ref_id'],'adjacent':0},call_id='read'+str(len(reads)))
        if len(reads)<2:
            query='method retrieves previous task feedback' if not reads else 'Scanned tables equations manual verification limitations'
            return ModelDecision('tool_call',tool_name='retrieve',arguments={'query':query,'corpus':'documents'},call_id='retrieve'+str(len(reads)))
        return ModelDecision('final',content='\n\n'.join(d['content']+' ['+d['evidence_id']+']' for d in reads))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    parser.add_argument('--model',default='sudocode-luna');parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    tag='v2-'+('live' if args.live else 'offline')+'-'+time.strftime('%Y%m%d-%H%M%S')
    folder=ROOT/'data'/'v2-eval'/tag;folder.mkdir(parents=True)
    output=args.output or ROOT/'evals'/'reports'/(tag+'.json')
    load_dotenv(args.env_file)
    model=lambda:model_from_env(args.model) if args.live else LocalWorkflowModel()
    app=Workbench(WorkbenchStore(folder/'workbench.db'),model,lambda observer:None,folder/'traces',start_worker=False)
    store=app.store;space=store.save_space({'name':'V2 验收资料','download_root':str(folder/'papers'),'download_count':2,'download_mb':30})
    conversation=store.create_conversation(space['id'],'资料与问答')
    rows=[]
    def run(name,action):
        started=time.monotonic()
        try:
            details=action();passed=details.pop('passed',True)
        except Exception as exc:
            passed=False;details={'error':str(redact(str(exc)))[:800]}
        row={'case':name,'passed':passed,'seconds':round(time.monotonic()-started,2),**details};rows.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
    def material(url,download=True):
        job=app.add_material(space['id'],{'conversation_id':conversation['id'],'url':url,'download':download,'topic':'V2 原文验收'})
        app.execute(store.claim_next());done=store.job(space['id'],job['id'])
        return {'passed':done['status']=='completed','job_id':job['id'],'status':done['status'],'error':done['error']}
    try:
        if args.live:
            run('real_paper_download_analysis',lambda:material('https://arxiv.org/abs/2408.09869'))
            run('real_paper_dedup',lambda:material('https://arxiv.org/abs/2408.09869'))
            run('github_read_only',lambda:material('https://github.com/docling-project/docling',False))
        else:
            app.library.fetch=lambda url,**kwargs:(pdf_bytes(),'application/pdf',url,'utf-8')
            run('paper_download_analysis',lambda:material('https://example.org/paper.pdf'))
            run('paper_dedup',lambda:material('https://example.org/paper.pdf'))
        def answer():
            job=store.enqueue(space['id'],conversation['id'],'只根据本地论文，说明该方法的设计和局限，附原文位置。','本地证据问答',[],kind='LOCAL_QA')
            app.execute(store.claim_next());done=store.job(space['id'],job['id'])
            result=store.get(done['run_id']) if done.get('run_id') else None
            evidence=(result or {}).get('evidence',[])
            read_cited=[e for e in evidence if e['kind'].startswith('local:D') and '['+e['evidence_id']+']' in done['summary']]
            return {'passed':done['status']=='completed' and len(read_cited)>=2,'original_citations':len(read_cited),'job_id':job['id'],'status':done['status'],'answer':done['summary'],'error':done['error']}
        run('local_question_with_quotes',answer)
        def failure():
            saved=app.library.fetch
            app.library.fetch=lambda *a,**k:(_ for _ in ()).throw(ValueError('HTTP 403: paywall fixture'))
            try:
                result=material('https://example.org/paywall.pdf');result['passed']=result['status']=='failed';return result
            finally:app.library.fetch=saved
        run('inaccessible_source_is_visible',failure)
        other=store.save_space({'name':'隔离资料区'})
        run('space_isolation',lambda:{'passed':not app.library.list(other['id'])})
        items=app.library.list(space['id'])
        report={'version':'v2','mode':'live' if args.live else 'offline','created_at':now_iso(),'cases':rows,'passed':all(r['passed'] for r in rows),
                'materials':items,'database':str(store.path),'boundary':'通过率仅表示工程流程；未作论文结论正确率评测。'}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Report: '+str(output),flush=True)
        return 0 if report['passed'] else 1
    finally:app.close()


if __name__=='__main__':raise SystemExit(main())
