"""Versioned feedback panels; old questions remain a separate historical control.

The authoring evidence is checked against the frozen original text before a run.
Only dev rows are copied by the experiment host into the coding workspace.
"""
import json

from .experiment_process import ROOT
from .experiment_retrieval import frozen_data as historical_data, sha

VERSION = '20260925-fact-groups-r2'
DEV_PANEL = ROOT / 'datasets/retrieval/retrieval_feedback_dev_v4.json'
FINAL_PANEL = ROOT / 'datasets/retrieval/retrieval_feedback_final_v4.json'
CORPUS_SNAPSHOT = ROOT / 'datasets/retrieval/retrieval_corpus_v3.json'


def frozen_data():
    historical, historical_hash = historical_data()
    snapshot = CORPUS_SNAPSHOT.read_bytes()
    source_hashes = {source['key']: source['sha256'] for source in json.loads(snapshot)}
    development = json.loads(DEV_PANEL.read_text(encoding='utf-8'))
    final = json.loads(FINAL_PANEL.read_text(encoding='utf-8'))
    questions = development + final
    if not development or not final:
        raise ValueError('开发题和终验题均不能为空')
    if any(q['split'] != 'dev' for q in development) or any(q['split'] != 'held_out' for q in final):
        raise ValueError('题目出现在错误的冻结切分')
    if {q['category'] for q in development} != {'direct', 'semantic', 'cross_document', 'unanswerable'}:
        raise ValueError('开发集缺少必要题型')
    all_questions = historical['questions'] + questions
    for field in ('id', 'query'):
        values = [q[field] for q in all_questions]
        if len(values) != len(set(values)):
            raise ValueError('新题与历史题或其他题重复')
    groups = [{group for q in panel for group in q['fact_groups']} for panel in (development, final)]
    if groups[0] & groups[1]:
        raise ValueError('开发与终验存在相同事实簇')
    chunk_sets = [{(g['document'], g['ordinal']) for q in panel for g in q['gold']}
                  for panel in (historical['questions'], development, final)]
    if any(chunk_sets[a] & chunk_sets[b] for a, b in ((0, 1), (0, 2), (1, 2))):
        raise ValueError('新增切分与历史题或开发题重复使用标注片段')
    docs = {doc['id']: doc for doc in historical['corpus']}
    for q in questions:
        if q['category'] == 'unanswerable':
            if q['gold'] or q['evidence'] or q['fact_groups'] or not q['annotation_note']:
                raise ValueError('无答案题需要空证据和明确标注依据')
            continue
        if not q['gold'] or len(q['gold']) != len(q['evidence']):
            raise ValueError('可回答题缺少逐项原文依据')
        if q['fact_groups'] != [e['group'] for e in q['evidence']]:
            raise ValueError('事实簇与逐项原文依据不一致')
        if len(set(q['fact_groups'])) != len(q['fact_groups']):
            raise ValueError('单题事实簇不能重复')
        if q['category'] == 'cross_document' and len({g['document'] for g in q['gold']}) < 2:
            raise ValueError('跨文档题必须包含至少两个来源')
        for gold, evidence in zip(q['gold'], q['evidence']):
            key = f"{gold['document']}:{gold['ordinal']}"
            if (key not in docs or evidence['document'] != gold['document'] or
                    evidence['ordinal'] != gold['ordinal'] or not evidence['quote'] or
                    evidence['quote'] not in docs[key]['content'] or evidence['url'] != docs[key]['url'] or
                    evidence.get('page') != docs[key]['page'] or
                    evidence.get('source_sha256') != source_hashes.get(gold['document']) or
                    evidence.get('chunk_sha256') != sha(docs[key]['content'])):
                raise ValueError('标注依据与冻结原文不一致')
    data = {'corpus': historical['corpus'], 'questions': questions,
            'dataset_info': {'version': VERSION, 'dev_questions': len(development), 'final_questions': len(final),
                             'historical_questions': len(historical['questions']), 'historical_hash': historical_hash,
                             'source_snapshot_hash': sha(snapshot),
                             'boundary': 'New disjoint fact-group panels on existing public sources; old 60 questions are seen historical regression, not this final split.'}}
    return data, sha(json.dumps(data, ensure_ascii=False, sort_keys=True))
