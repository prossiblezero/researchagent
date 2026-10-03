"""Join completed, separately frozen cohorts without mixing their task scores."""
import collections
import hashlib
import json
import re
import statistics
from pathlib import Path
from evals import run_mixed_benchmark as b


def normalize_judgments(root):
    """Preserve paid receipts and schema failures; interpret explicit added-claim lists/text."""
    for path in root.glob('qa/*/*/judgment.json'):
        original=b.read(path)
        if original.get('error')!='Invalid judge schema':continue
        receipts=sorted((path.parent/'judge/requests').glob('*.json'))
        if not receipts:continue
        receipt=b.read(receipts[-1])
        response=receipt.get('response',{}).get('content','')
        try:value=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',response.strip()))
        except ValueError:continue
        additions=value.get('unsupported_additions')
        if not isinstance(additions,(list,str)) or not all(type(value.get(k)) is bool for k in ['correct','citation_sufficient','refused']):continue
        value['unsupported_additions']=bool(additions)
        value['normalization']={'field':'unsupported_additions','original':additions,
            'rule':'empty list/text means no listed additions; nonempty list/text means listed additions; original schema failure retained'}
        answer=(path.parent/'answer.md').read_text(encoding='utf-8')
        try:submitted=json.loads(receipt['messages'][-1]['content'])['answer']
        except (KeyError,IndexError,TypeError,ValueError):continue
        if submitted!=answer:continue  # A changed artifact must not inherit an old judge response.
        score=b.read(path.parent/'score.json')
        value.update(valid=True,answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
            task_pass=score.get('product_completed',False) and value['correct'] and value['citation_sufficient'] and not value['unsupported_additions'])
        b.write(path.parent/'judge-schema-failure.json',original)
        b.write(path,value)


def build(root=b.DEFAULT):
    qa_root=root/'deepseek-qa';condition=b.read(qa_root/'condition.json')
    normalize_judgments(qa_root)
    qa_metrics=b.report(qa_root,condition)
    rows=[]
    for name in b.DATASET_NAMES:
        source=root/('paired-history' if name=='longmemeval' else 'gpu-retrieval')
        for path in (source/'retrieval'/name).glob('*/*.json'):
            rows.append({**b.read(path),'artifact':path.relative_to(root).as_posix()})
    groups=collections.defaultdict(list)
    for row in rows:groups[(row['dataset'],row['split'],row['arm'])].append(row)
    retrieval=[{'dataset':key[0],'split':key[1],'arm':key[2],'tasks':len(items),
        'eligible':sum(r['eligible'] for r in items),'failures':sum(r['status']!='completed' for r in items),
        **{metric:b.mean(items,metric) for metric in ['recall_at5','mrr_at5','all_gold_at5']}}
        for key,items in sorted(groups.items())]
    answers=b.read(qa_root/'qa-results.json')
    requests=[]
    for row in answers:
        paths=(qa_root/row['artifact']/'requests').glob('*.json')
        receipts=[b.read(p) for p in paths]
        for request in receipts:
            requests.append({'id':row['id'],'arm':row['arm'],'purpose':request.get('purpose'),
                             'error':request.get('error'),'finished':bool(request.get('finished_at'))})
        row['provider_errors']=[{'purpose':r.get('purpose'),'error':r['error']} for r in receipts if r.get('error')]
        row['actual_source_links']='deepseek-qa/'+row['artifact']+'/run.json'
    diagnostics=[]
    for arm in b.qa.ARMS:
        subset=[r for r in answers if r['arm']==arm]
        diagnostics.append({'arm':arm,'attempts':len(subset),
            'multiple_tool_call_failure_runs':sum(any('multiple tool calls' in e['error'] for e in r['provider_errors']) for r in subset),
            'transport_failure_runs':sum(any(any(x in e['error'] for x in ['overloaded','超时','中断','HTTP']) for e in r['provider_errors']) for r in subset),
            'completed_seconds_mean':b.mean([r for r in subset if r.get('product_completed')],'seconds'),
            'terminations':dict(collections.Counter(r.get('termination',r['status']) for r in subset))})
    complete=len(rows)==592 and len(answers)==64 and all(r['judgment']['valid'] for r in answers) and qa_metrics['workflow']['executed']==24
    result={'status':'complete' if complete else 'in_progress_or_ungraded',
        'retrieval':retrieval,'qa':qa_metrics['qa'],'workflow':qa_metrics['workflow'],'diagnostics':diagnostics,
        'luna_pilot':b.read(root/'luna-stop.json'),'retrieval_runs':len(rows),'qa_runs':len(answers),
        'history_control':b.read(root/'paired-history/baseline-reproduction.json') if (root/'paired-history/baseline-reproduction.json').exists() else None}
    b.write(root/'metrics.json',result);b.write(root/'retrieval-results.json',rows)
    b.write(root/'qa-results.json',answers)
    b.write(root/'failures.json',{'retrieval':[r for r in rows if r['status']!='completed' or r['recall_at5']==0],
        'qa':[r for r in answers if not r['judgment'].get('task_pass')],'model_requests':[r for r in requests if r['error'] or not r['finished']]})
    pct=lambda v:'—' if v is None else f'{v:.2%}'
    decimal=lambda v:'—' if v is None else f'{v:.4f}'
    md='# ResearchAgent 真实产品评测\n\n'
    md+=f"状态：**{result['status']}**。320条固定任务：296条实际检索任务、24项可执行工程回归；另从中冻结32题做64次完整问答。每个轨道单独报告，不合成一个总分。\n\n"
    totals={r['arm']:r for r in qa_metrics['qa'] if r['dataset']=='all'}
    md+=f"**本轮结果：混合检索有量化收益，完整问答未改善。** SciFact有证据保留题Recall@5为75.00%→87.50%，QASPER完整映射保留题为44.46%→52.32%。DeepSeek完整问答通过{totals['baseline']['passed']}/{totals['baseline']['attempts']}→{totals['candidate']['passed']}/{totals['candidate']['attempts']}；当前工具协议适配和最终引用绑定是主要阻塞点。检索指标可用于限定范围的简历表述，不能替代整体Agent通过率。[失败分析与下一步](findings.md)\n\n"
    md+='## 生产检索\n\n'
    md+='两臂直接调用生产 `Retriever.retrieve`。baseline=FTS5/BM25，candidate=FTS5+BGE-M3+RRF；包含各自生产分段。相同公开语料、实际返回top5和2000 token预览预算，按父片段/会话去重。MRR为MRR@5。无证据题不计召回，空检索不能代表正确拒答。\n\n'
    md+='SciFact使用全部5,183篇摘要；QASPER使用目标论文全文文本、摘要与图表说明，17条证据映射不完整任务保留但不计召回；HotpotQA使用每题原有10篇上下文；LongMemEval使用完整历史干扰会话，导入真实独立会话并显式跨会话检索，未评其完整答案准确率。项目题均为历史回归。\n\n'
    md+='|数据|切分|策略|任务/可评分|Recall@5|MRR@5|运行失败|\n|---|---|---|---:|---:|---:|---:|\n'
    for r in retrieval:md+=f"|{r['dataset']}|{r['split']}|{r['arm']}|{r['tasks']}/{r['eligible']}|{pct(r['recall_at5'])}|{decimal(r['mrr_at5'])}|{r['failures']}|\n"
    md+='\n历史检索两臂从同一SQLite来源快照复制，固定会话与消息ID，避免同分排序的随机ID混杂；先前独立导入版本保留但不用于上表。其他文档轨道的段落ID按固定导入顺序生成。所有数据是固定子集，不是官方榜单成绩。\n\n'
    md+='## 实际 Agent 问答\n\n'
    md+=f"完整配对使用 `.env` 中的 **{condition['model_settings']['name']}**，两臂同模型、quick预算、混合检索与CUDA环境。baseline关闭原文重排/阅读提示；candidate开启二者。执行 Workbench→LOCAL_QA→检索→读取原文→引用核验→保存报告/记忆；意图路由已指定，不计路由正确率。每题每臂独立SQLite，仅共用不可变的公开来源向量。gold不进入生成链。\n\n"
    md+='问答评分使用独立模型调用，隐藏策略名，依据标准答案和实际引用原文；这不是独立人类评分。通过要求产品完成、回答正确、引用充分、没有无据添加。计入所有首次失败。正确草稿未完成核验仍不算通过，读过原文也不代表最终引用绑定正确。部分评分把unsupported_additions返回为列表/文字，按有无列出添加项归一为布尔值；保留原始响应和首次schema失败，不重复调用模型。\n\n'
    md+='|任务组|策略|完成/尝试|盲评/尝试|通过|语义正确|引用充分|整段阅读率|报告token/完整计量运行|\n|---|---|---:|---:|---:|---:|---:|---:|---:|\n'
    for r in qa_metrics['qa']:
        md+=f"|{r['dataset']}|{r['arm']}|{r['completed']}/{r['attempts']}|{r['judged']}/{r['attempts']}|{r['passed']}|{r['correct']}|{r['citation_sufficient']}|{pct(r['gold_fully_read_passage_rate'])}|{r['reported_tokens']}/{r['usage_complete_runs']}|\n"
    md+='\n整段阅读仅按成功回答/修复请求中真实可见的read_evidence窗口计算；后台重排、核验器或证据库保存全文不计入。失败任务会降低阅读率，因此它包含执行失败影响，不能单独解释为阅读策略的因果提升。已报告token不含独立评分调用；评分请求另存各judge/requests。失败快速返回会拉低全样本平均延迟，不能把它写成性能优化。\n\n'
    for r in diagnostics:md+=f"- {r['arm']}：多工具协议失败涉及 {r['multiple_tool_call_failure_runs']} 次运行；传输/服务异常涉及 {r['transport_failure_runs']} 次运行。终止原因：`{json.dumps(r['terminations'],ensure_ascii=False)}`。\n"
    pilot=result['luna_pilot']
    md+=f"\nLuna试跑单列：{pilot['completed_attempt_records']}条首轮记录、{pilot['requests']}次请求、{pilot['errors']}条异常、{pilot['unfinished_receipts']}条未完成响应收据。因跨题持续过载和流中断停止；使用用户已授权的DeepSeek重建独立32题协议，不合并模型成绩，不重放未知在途请求。[停止记录](luna-stop.json) · [Luna原始运行](qa/)\n\n"
    md+=f"## 工程回归\n\n24项产品场景通过 {qa_metrics['workflow']['passed']}/{qa_metrics['workflow']['executed']}，调用tests下实际方法验证独立会话、分支、记忆作用域、版本管理、证据锚点和上下文恢复；没有把字符串非空当成功。完整后端与前端回归记录见[regression.json](regression.json)。\n\n"
    md+='## 可查阅证据\n\n[完整指标](metrics.json) · [检索逐题](retrieval-results.json) · [问答逐题](qa-results.json) · [失败明细](failures.json) · [数据缺失](data-audit.json) · [固定问答协议](deepseek-qa/condition.json) · [实际设备](deepseek-qa/environment.json) · [同ID历史协议](paired-history/paired-condition.json)\n\n'
    for r in answers:
        base='deepseek-qa/'+r['artifact']
        md+=f"- {r['id']} / {r['arm']}：{r['status']}，通过={r['judgment'].get('task_pass','未评分')}。[回答]({base}/answer.md) · [来源/结论]({base}/run.json) · [轨迹]({base}/events.json) · [原文窗口/用量]({base}/score.json) · [评分]({base}/judgment.json)\n"
    (root/'report.md').write_text(md,encoding='utf-8');(root/'report.html').write_text(b.render_report(md,'真实产品评测'),encoding='utf-8')
    return result


if __name__=='__main__':
    result=build();print(json.dumps({'status':result['status'],'retrieval_runs':result['retrieval_runs'],'qa_runs':result['qa_runs']},ensure_ascii=False))
