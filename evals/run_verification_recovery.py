"""Bounded replay of retained public failures; never loads the user database.

Three saved repaired drafts, paired old/new verification; three negative controls;
one injected verification outage followed by a real-model checkpoint recovery.
This isolates final verification and is not a new end-to-end retrieval benchmark.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict, replace
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from research_agent import load_dotenv
from research_agent.contracts import Evidence, Source, ModelDecision
from research_agent.loop import ResearchAgent
from research_agent.models import model_from_env
from research_agent.reports import render_report
from research_agent.trace import redact
from research_agent.usage import summarize_usage
from research_agent.verify import check_answer
from evals.run_harness_strategies import LIVE

HISTORY=ROOT/'evals/reports/harness-strategies-20260921/release-live/live'
BASE='9e5c093'
CASES=[('react-tasks','candidate'),('quoted-injection','baseline'),('quoted-injection','candidate')]


def write(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def manifest():
    paths=[*ROOT.glob('research_agent/*.py'),ROOT/'web/app.js',Path(__file__)]
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def assert_same_product(previous,current):
    def product(data):
        return {k.replace('\\','/'):v for k,v in data.items() if k.replace('\\','/').startswith('research_agent/')}
    before,after=product(previous),product(current)
    if not before or before!=after:
        raise ValueError('Product code changed or manifest is empty; do not mix saved evaluation results')


def load_case(case,arm,repaired=True):
    folder=HISTORY/case/arm
    events=json.loads((folder/'events.json').read_text(encoding='utf-8'))
    run=json.loads((folder/'run.json').read_text(encoding='utf-8'))
    drafts=[e for e in events if e['event']=='answer_draft']
    selected=drafts[1 if repaired else 0]
    # Do not silently introduce evidence collected after the replayed draft.
    assert not any(e['seq']>selected['seq'] and e['event']=='tool_call_requested' for e in events)
    question=next(c['query'] for c in LIVE if c['id']==case)
    return question,selected['text'],[Evidence(**e) for e in run['evidence']],[Source(**s) for s in run['sources']],run


def recorded_model(profile,folder):
    model=model_from_env(profile);complete=model.complete;requests=[]
    def recorded(messages,tools):
        row={'purpose':model.usage_purpose,'messages':messages,'tools':tools};requests.append(row)
        write(folder/'requests.json',redact(requests))
        try:
            decision=complete(messages,tools);row['response']=asdict(decision)
            return decision
        except Exception as exc:
            row['error']=redact(str(exc));raise
        finally:write(folder/'requests.json',redact(requests))
    model.complete=recorded
    return model


def build_report(out,metrics):
    rows=metrics['rows'];recovery=metrics['recovery']
    md='# 列表引用与核验恢复：限定真实验收\n\n'
    md+=f'历史修订稿配对核验：修复前 **{metrics["paired_before_pass"]}/3**，修复后 **{metrics["paired_after_pass"]}/3**；负例拦截 **{metrics["negative_rejections"]}/3**。模型：{rows[0]["model"]}。\n\n'
    md+='固定使用三条历史失败的第1次修订稿、相同已读原文与核验预算，交替运行前后版本。仅比较最后的引用绑定与核验；不改写旧16次结果，不将此结果当成新的检索成功率。\n\n'
    md+='| 案例 | 版本 | 核验通过 | 秒 | 产物 |\n|---|---|---|---|---|\n'
    for r in rows:md+=f'| {r["case"]} | {r["arm"]} | {r["ready"]} | {r["seconds"]} | [草稿与原文]({r["case"]}/{r["arm"]}/report.html) · [核验]({r["case"]}/{r["arm"]}/check.json) · [请求]({r["case"]}/{r["arm"]}/requests.json) |\n'
    md+='\n## 恢复\n\n'
    md+=f'用之前真实超时的 precision/baseline 草稿和证据重建检查点，先注入一次核验中断，再以真实模型续跑一次。结果：{recovery["status"]}；整份草稿不变：{recovery["draft_preserved"]}；新增检索/阅读 {recovery["new_tool_calls"]} 次，重新生成整份答案 {recovery["answer_generation_calls"]} 次，局部修订 {recovery["local_repair_calls"]} 次，复用已通过段落 {recovery["verified_blocks_reused"]} 段。这不表示真实服务商以后不会超时。\n\n'
    md+='[恢复后的答案](precision-recovery/answer.html) · [轨迹](precision-recovery/events.json) · [指标和用量](metrics.json) · [固定条件](condition.json)\n\n'
    md+='原文来自已有公开论文快照，包含解析位置、来源和内容哈希；核验请求实际送入的摘录可在 requests.json 查看。没有新增下载，也没有通读全文。原先跨文档架构漏读问题不属于本次修复，仍保留在旧报告。超时缺失 token 仍按未知计量，不填零；旧策略自动启用结论保持。\n'
    (out/'report.md').write_text(md,encoding='utf-8')
    public_root=ROOT/'evals/reports/harness-strategies-20260921'
    if out.is_relative_to(public_root):
        base='http://127.0.0.1:8000/harness-evaluation/'+out.relative_to(public_root).as_posix()+'/'
        md=re.sub(r'\]\((?!https?://)([^)]+)\)',lambda m:']('+base+m[1]+')',md)
    (out/'report.html').write_text(render_report(md,'引用与恢复验收'),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--model',default='sudocode-luna')
    parser.add_argument('--resume',action='store_true',help='Continue an interrupted run without repeating saved checks')
    args=parser.parse_args();out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=args.resume)
    load_dotenv(ROOT/'.env')
    old_source=subprocess.check_output(['git','show',BASE+':research_agent/verify.py'],cwd=ROOT).decode('utf-8')
    old=types.ModuleType('research_agent._baseline_verify');old.__package__='research_agent'
    exec(compile(old_source,BASE+':verify.py','exec'),old.__dict__)
    start=manifest()
    if args.resume:
        condition=json.loads((out/'condition.json').read_text(encoding='utf-8'))
        assert condition['model_profile']==args.model and condition['baseline_commit']==BASE
        prior=json.loads((out/'start-manifest.json').read_text(encoding='utf-8'))
        assert_same_product(prior,start)
        write(out/'resume-manifest.json',start)
    else:
        write(out/'start-manifest.json',start)
        write(out/'condition.json',{'model_profile':args.model,'baseline_commit':BASE,
        'baseline_verifier_sha256':hashlib.sha256(old_source.encode()).hexdigest(),
        'cases':CASES,'paired_draft_index':1,'input_limit':14080,
        'protocol':'Same saved repaired draft and original evidence; alternating order; no additional retrieval; one attempt per arm, provider bounded retry unchanged.',
        'negative_controls':['wrong_fact','wrong_source','snippet_only'],
        'recovery':'Reconstructed checkpoint from retained precision/baseline draft. Inject one outage, then resume once with real model. Not a natural provider timeout rate measurement.'})
    rows=[]
    def evaluate(label,arm,question,draft,evidence,sources,checker,expected):
        folder=out/label/arm
        if args.resume and (folder/'metrics.json').exists():
            rows.append(json.loads((folder/'metrics.json').read_text(encoding='utf-8')));return
        folder.mkdir(parents=True)
        (folder/'draft.md').write_text(draft,encoding='utf-8')
        write(folder/'evidence.json',{'question':question,'sources':[asdict(s) for s in sources],'evidence':[asdict(e) for e in evidence]})
        model=recorded_model(args.model,folder);started=time.perf_counter();error='';check={}
        try:check=checker(model,question,draft,evidence,sources,input_limit=14080)
        except (ValueError,RuntimeError,TypeError,TimeoutError) as exc:error=redact(str(exc))
        write(folder/'check.json',check);write(folder/'usage.json',model.usage_records)
        row={'case':label,'arm':arm,'model':model.name,'ready':check.get('ready'),
            'expected_ready':expected,'matches_expectation':not error and check.get('ready')==expected,
            'seconds':round(time.perf_counter()-started,3),'error':error,
            'supported_blocks':sum(c['supported'] for c in check.get('claims',[])),
            'blocks':len(check.get('blocks',[])),'usage':summarize_usage(model.usage_records)}
        write(folder/'metrics.json',row);rows.append(row)
        detail='# '+label+' / '+arm+'\n\n这是已保存草稿的核验回放，不是重新检索。\n\n'+draft
        detail+='\n\n## 核验\n\n'+json.dumps(row,ensure_ascii=False,indent=2)
        for e in evidence:
            if e.kind.startswith('local:'):
                detail+='\n\n## '+e.evidence_id+' '+e.title+'\n\n解析位置：'+json.dumps(e.provenance,ensure_ascii=False)+'\n\n'+e.content
        (folder/'report.html').write_text(render_report(detail,label+' / '+arm),encoding='utf-8')
        print(json.dumps({k:row[k] for k in ('case','arm','ready','seconds','error')},ensure_ascii=False),flush=True)

    for index,(case,arm) in enumerate(CASES):
        question,draft,evidence,sources,_=load_case(case,arm)
        pair=[('before',old.check_answer),('after',check_answer)]
        for version,checker in pair if index%2==0 else reversed(pair):
            evaluate(case+'-'+arm,version,question,draft,evidence,sources,checker,True)
    question,draft,evidence,sources,_=load_case('quoted-injection','baseline')
    evaluate('wrong_fact','after',question,draft.replace('WebShop','ImageNet'),evidence,sources,check_answer,False)
    _,_,other_evidence,other_sources,_=load_case('precision','baseline',False)
    evaluate('wrong_source','after',question,draft,[e for e in other_evidence if e.evidence_id=='E7'],other_sources,check_answer,False)
    evaluate('snippet_only','after',question,draft,[replace(e,kind='snippet') for e in evidence],sources,check_answer,False)

    question,draft,evidence,sources,run=load_case('precision','baseline',False)
    folder=out/'precision-recovery';folder.mkdir(exist_ok=args.resume)
    initial={'question':question,'messages':[{'role':'user','content':question}],
        'sources':[asdict(s) for s in sources],'evidence':[asdict(e) for e in evidence],
        'counters':[run[k] for k in ('tool_calls','tool_attempts','network_requests','tool_denials','tool_successes')],
        'cache':[],'next_iteration':0,'pending_decision':asdict(ModelDecision('final',content=draft))}
    class Outage:
        name='injected-outage';supports_answer_verification=True;usage_purpose='answer'
        def complete(self,messages,tools):
            assert self.usage_purpose=='answer_verification'
            raise TimeoutError('Injected verification outage; no provider request made')
    states=[]
    if args.resume and (folder/'saved-checkpoint.json').exists():
        states.append(json.loads((folder/'saved-checkpoint.json').read_text(encoding='utf-8')))
        failed=json.loads((folder/'injected-result.json').read_text(encoding='utf-8'))
        assert not (folder/'run.json').exists(), 'Recovery already completed; do not rerun'
    else:
        write(folder/'reconstructed-checkpoint.json',initial)
        failed=asdict(ResearchAgent(None,Outage(),folder/'injected-traces',max_tool_calls=8,resume_state=initial,state_callback=states.append,allow_external=False).run(question))
        write(folder/'injected-result.json',failed);write(folder/'saved-checkpoint.json',states[-1])
    model=recorded_model(args.model,folder);events=[];started=time.perf_counter()
    resumed=ResearchAgent(None,model,folder/'resume-traces',max_tool_calls=8,max_rounds=1,
        resume_state=states[-1],allow_external=False,semantic_compaction=True,on_event=events.append).run(question)
    recovery={'status':resumed.status,'termination':resumed.termination,'seconds':round(time.perf_counter()-started,3),
        'injected_outage_saved':failed['termination']=='answer_verification_unavailable',
        'draft_preserved':resumed.answer==draft,
        'new_tool_calls':sum(e['event']=='tool_call_requested' for e in events),
        'new_generation_calls':sum(e['event']=='model_request' for e in events),
        'answer_generation_calls':sum(r['purpose']=='answer' for r in json.loads((folder/'requests.json').read_text(encoding='utf-8'))),
        'local_repair_calls':sum(r['purpose']=='answer_repair' for r in json.loads((folder/'requests.json').read_text(encoding='utf-8'))),
        'verified_blocks_reused':sum(e.get('reused_blocks',0) for e in events if e['event']=='answer_checked'),
        'usage':summarize_usage(model.usage_records)}
    write(folder/'run.json',asdict(resumed));write(folder/'events.json',events);write(folder/'usage.json',model.usage_records)
    write(folder/'metrics.json',recovery);(folder/'answer.md').write_text(resumed.answer,encoding='utf-8')
    (folder/'answer.html').write_text(render_report(resumed.answer,'核验恢复结果'),encoding='utf-8')
    finish=manifest();write(out/'end-manifest.json',finish)
    metrics={'paired_before_pass':sum(r['ready'] is True for r in rows if r['arm']=='before'),
        'paired_after_pass':sum(r['ready'] is True for r in rows if r['arm']=='after' and r['expected_ready']),
        'paired_total':3,'negative_rejections':sum(r['matches_expectation'] for r in rows if not r['expected_ready']),
        'negative_total':3,'source_unchanged_during_run':start==finish,'rows':rows,'recovery':recovery}
    write(out/'metrics.json',metrics)
    build_report(out,metrics)
    print(json.dumps({k:v for k,v in metrics.items() if k!='rows'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
