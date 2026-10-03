"""60 frozen development/held-out retrieval questions over actual source snapshots.

Held-out means not used to tune retrieval parameters, not a public leaderboard.
Gold evidence is selected from original sources before either retriever runs.
"""
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from research_agent.workbench_store import WorkbenchStore
from research_agent.library import Library
from research_agent.materials import split_pages
from research_agent.retrieval import Retriever
from research_agent.reports import render_report


def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')


def dataset():
    if (ROOT/'datasets/retrieval/retrieval_corpus_v3.json').is_file():
        return json.loads((ROOT/'datasets/retrieval/retrieval_corpus_v3.json').read_text(encoding='utf-8')),json.loads((ROOT/'datasets/retrieval/retrieval_questions_v3.json').read_text(encoding='utf-8'))
    papers=json.loads((ROOT/'datasets/research/local_corpus_v1.json').read_text(encoding='utf-8'))
    docs=[dict(p,key=k) for k,p in zip(('docling','react'),papers)]
    sources=Path(os.environ['TEMP'])/'researchagent-design-research-20260918/sources'
    for key,relative,url in (
        ('pi','pi/packages/coding-agent/docs/compaction.md','https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md'),
        ('codex','codex/codex-rs/memories/README.md','https://github.com/openai/codex/blob/main/codex-rs/memories/README.md'),
        ('openclaw','openclaw/docs/concepts/memory-search.md','https://github.com/openclaw/openclaw/blob/main/docs/concepts/memory-search.md'),
        ('hermes','hermes/agent/micro_compaction.py','https://github.com/NousResearch/hermes-agent/blob/main/agent/micro_compaction.py')):
        data=(sources/relative).read_bytes();text=data.decode('utf-8')
        docs.append({'key':key,'title':key+' / '+relative.split('/')[-1],'url':url,'sha256':hashlib.sha256(data).hexdigest(),'metadata':{'source':'official source snapshot, retrieved 2026-09-18'},
                     'chunks':[dict(c,content=c['text']) for c in split_pages([(None,text)])]})
    topics=[
      ('docling',5,'Docling RT-DETR architecture layout detector','版面识别的目标检测器是从哪种架构改造的？'),
      ('docling',6,'Docling onnxruntime 72 dpi inference','文档转换工具将页面图片以什么分辨率送入模型，用哪个运行时推理？'),
      ('docling',6,'Docling TableFormer PyTorch table structure','如何恢复表格的逻辑行列和表头结构，负责这个任务的模型用什么框架运行？'),
      ('docling',7,'Docling TableFormer matched PDF cells re-transcription','表格结构预测后怎样与已有文字单元对齐，从而避免重复识别图片中的文字？'),
      ('docling',8,'Docling BaseModelPipeline extensibility','如果希望给文档转换添加自己的模型组件，哪个扩展接口可以定制整个处理流程？'),
      ('docling',9,'Docling pypdfium low-resource quality tradeoff','内存和算力很少时可以换什么解析后端，其速度与提取质量之间有什么取舍？'),
      ('docling',10,'Docling 225 pages OCR disabled benchmark','这份文档处理报告的计时实验用了多少页，字符识别功能是否开启？'),
      ('docling',3,'Docling JSON Markdown metadata OCR','文档转换会输出哪些机器可读格式，还能取出哪些元信息？'),
      ('docling',4,'Docling CPU threads runtime options Dockerfile','用户能控制哪些运行时开关和线程预算，有没有容器运行示例？'),
      ('docling',1,'Docling MIT licensed commodity hardware','这个PDF转换包使用什么开源许可，对硬件资源有什么要求？'),
      ('react',6,'ReAct interleaved reason to act act to reason','交替思考和操作如何使高层计划随环境反馈动态调整？'),
      ('react',7,'ReAct HotpotQA Fever ALFWorld WebShop benchmarks','把推理和行动结合的论文在哪四类知识和交互任务上进行了评价？'),
      ('react',12,'ReAct HotpotQA Fever 6 3 few-shot exemplars','知识问答和事实核验的提示中分别用了多少个人工编写的示例轨迹？'),
      ('pi','contextTokens > contextWindow - reserveTokens','pi contextTokens contextWindow reserveTokens auto-compaction','这个编码助手在工具返回后，依据什么条件判断该腾出上下文空间？'),
      ('pi','Never cut at tool results','pi cut point tool results compaction','压缩旧对话时，为什么不能把工具返回单独留在另一边？'),
      ('pi','firstKeptEntryId','pi repeated compactions firstKeptEntryId','第二次压缩如何继续覆盖上次留下的消息，避免把仍有用的历史遗漏掉？'),
      ('codex','Phase 1: Rollout Extraction','codex Phase 1 rollout extraction eligible idle','跨会话记忆的第一阶段怎样挑选已经空闲的对话并提取结构化记录？'),
      ('codex','single global phase-2 lock','codex Phase 2 global consolidation lock','多个记忆任务并发运行时，怎样防止它们同时修改共享的整合结果？'),
      ('openclaw','BM25 keyword search','OpenClaw BM25 vector exact terms','当提问与原文措辞不同，但又包含必须精确匹配的错误码时，两条检索路径分别解决什么？'),
      ('hermes','OFF by default','Hermes micro-compaction prompt prefix cache','为什么逐轮重写摘要的优化默认关闭，它对供应商的提示缓存有什么影响？'),
    ]
    by_key={d['key']:d for d in docs};cases=[]
    gold=[]
    for i,(key,anchor,direct,semantic) in enumerate(topics):
        chunks=by_key[key]['chunks']
        if isinstance(anchor,int):indices=[anchor]
        else:
            indices=[j for j,c in enumerate(chunks) if anchor in c['content']]
            if not indices:raise ValueError('Missing gold anchor: '+anchor)
        expected=[{'document':key,'ordinal':j} for j in indices]
        gold.append(expected)
        for kind,query in [('direct',direct),('semantic',semantic)]:
            cases.append({'id':f'{kind}-{i:02}','category':kind,'split':'dev' if i<10 else 'held_out','query':query,'gold':expected})
    pairs=[(10,13),(10,18),(11,16),(12,14),(13,19),(14,17),(15,16),(16,18),(17,19),(18,19)]
    for i,(a,b) in enumerate(pairs):
        cases.append({'id':f'cross-{i:02}','category':'cross_document','split':'held_out','query':topics[a][3]+' 同时查找：'+topics[b][3],'gold':gold[a]+gold[b]})
    for i,query in enumerate([
        'Docling 未公开的逐客户合同收入和银行卡账号是什么？','ReAct 作者个人家庭住址','pi 内部未公开训练集精确大小与私有API密钥',
        'Hermes 2028年12月31日尚未发生的线上事故原因','OpenClaw 用户私有数据库密码','Codex 未公开每个客户的 GPU 账单',
        '这些资料里有没有不存在的算法 ZXQ-771 的正确率？','火星上的木屋壁炉2029年销售数据','请查这些文档未记载的量子芯片 Q-ABCD 配方','作者没有提供的盲审审稿人姓名']):
        cases.append({'id':f'absent-{i:02}','category':'unanswerable','split':'held_out','query':query,'gold':[]})
    return docs,cases


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    docs,cases=dataset();write(output/'corpus.json',docs);write(output/'questions.json',cases)
    write(output/'manifest.json',{'seed':20260918,'questions':60,'dev':20,'held_out':40,'grouping':'dev=Docling only; held_out=ReAct and source repos, never used for parameter selection',
        'gold_unit':'original document chunk; alternate segments deduplicated','unanswerable':'not scored as retrieval recall; an evidence sufficiency task for the agent',
        'source_license':'Source snapshots retain original licenses; task questions are CC0.'})
    rows=[];resources={}
    for mode in ['lexical','hybrid']:
        os.environ['RETRIEVAL_MODE']=mode
        store=WorkbenchStore(output/(mode+'.db'));space=store.save_space({'name':'Retrieval '+mode})['id'];lib=Library(store)
        chunk_map={}
        for d in docs:
            item,_=lib.save(space,{'kind':'paper' if d['key'] in ('docling','react') else 'document','title':d['title'],'url':d['url'],'canonical_id':d['key'],
                'metadata':d['metadata'],'data':None,'warnings':[],'boundary':'Frozen original source text',
                'chunks':[dict(c,text=c['content']) for c in d['chunks']]})
            for c in lib.chunks(space,item['id']):chunk_map[c['id']]=(d['key'],c['ordinal'])
        retriever=Retriever(store,lib);start=time.perf_counter();retriever.sync(space)
        resources[mode]={'setup_index_seconds':round(time.perf_counter()-start,3),'status':retriever.status(space)}
        for case in cases:
            response=retriever.retrieve(space,case['query'],top_k=10)
            ranking=list(dict.fromkeys(chunk_map[r['chunk_id']] for r in retriever.last_ranking))
            gold={(r['document'],r['ordinal']) for r in case['gold']}
            positive=[i+1 for i,key in enumerate(ranking) if key in gold]
            rows.append({'id':case['id'],'mode':mode,'split':case['split'],'category':case['category'],'latency_ms':response['latency_ms'],
                'recall5':len(gold & set(ranking[:5]))/len(gold) if gold else None,'recall10':len(gold & set(ranking[:10]))/len(gold) if gold else None,
                'mrr':1/min(positive) if positive else (0 if gold else None),'returned_hits':len(response['results']),
                'ranking':ranking[:10],'tool_output':response})
            print(json.dumps({'mode':mode,'id':case['id'],'recall10':rows[-1]['recall10'],'ms':response['latency_ms']}),flush=True)
    write(output/'results.json',rows)
    summary={'resources':resources,'metrics':[]}
    for mode in ['lexical','hybrid']:
        for split in ['dev','held_out']:
            selected=[r for r in rows if r['mode']==mode and r['split']==split and r['recall10'] is not None]
            times=sorted(r['latency_ms'] for r in selected)
            summary['metrics'].append({'mode':mode,'split':split,'answerable_questions':len(selected),
                **{metric:statistics.mean(r[metric] for r in selected) for metric in ('recall5','recall10','mrr')},'warm_p95_ms':times[int(.95*(len(times)-1))]})
    b1=next(m for m in summary['metrics'] if m['mode']=='lexical' and m['split']=='held_out')
    b2=next(m for m in summary['metrics'] if m['mode']=='hybrid' and m['split']=='held_out')
    summary['held_out_recall10_gain_pp']=100*(b2['recall10']-b1['recall10'])
    summary['quality_gate_5pp']=summary['held_out_recall10_gain_pp']>=5
    write(output/'metrics.json',summary)
    markdown='# 检索评测\n\n60 道固定问题；20 道开发题，40 道按文档隔离的保留题（含 10 道无答案题）。无答案题不计入召回率，需要由 Agent 判断证据是否充分。\n\n'
    markdown+='|方式|分组|Recall@5|Recall@10|MRR|热查询 P95|\n|---|---|---|---|---|---|\n'
    for m in summary['metrics']:markdown+=f"|{m['mode']}|{m['split']}|{m['recall5']:.1%}|{m['recall10']:.1%}|{m['mrr']:.3f}|{m['warm_p95_ms']:.0f} ms|\n"
    markdown+='\n召回率按原始片段计算，ANN 子片段去重。指标来自融合排序的前 k 个片段；给模型的工具输出仍受 2000 token 预算限制。此语料规模不能代表 10 万片段性能。\n'
    (output/'report.md').write_text(markdown,encoding='utf-8');(output/'report.html').write_text(render_report(markdown,'检索评测'),encoding='utf-8')


if __name__=='__main__':
    os.environ['BGE_MODEL_PATH']=str(ROOT/'data/models/bge-m3')
    import argparse
    from datetime import datetime
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'evals/reports'/('retrieval-'+datetime.now().strftime('%Y%m%d-%H%M%S')))
    run(parser.parse_args().output)
