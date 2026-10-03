"""Registered V4 benchmark: existing public source snapshots, trusted host scoring."""
import hashlib
import inspect
import json
import math
import platform
import statistics
import sys
from pathlib import Path

from .retrieval import lexical
from .experiment_process import ROOT, sandbox_command, run_process

SUITE = 'public-retrieval-v1'

BASELINE = '''import re
import sqlite3
from contextlib import closing

{lexical}

def rank(corpus, queries, top_k=10):
    """Existing workspace FTS5 branch: title/section/body weights 4/2/1."""
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE VIRTUAL TABLE chunks USING fts5(id UNINDEXED,title,section,body)')
        db.executemany('INSERT INTO chunks VALUES(?,?,?,?)', [(d['id'],lexical(d['title']),lexical(d['section']),lexical(d['content'])) for d in corpus])
        result=[]
        for query in queries:
            terms=list(dict.fromkeys(lexical(query).split()))[:64]
            expression=' OR '.join('"'+t+'"' for t in terms)
            hits=[r[0] for r in db.execute('SELECT id FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks,0,4,2,1) LIMIT ?', (expression,top_k))] if expression else []
            result.append(hits)
        return result
'''.replace('{lexical}', inspect.getsource(lexical))

# Gold labels and metric computation are deliberately outside the candidate process.
PREDICT = '''import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('candidate', 'ranker.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
payload=json.load(sys.stdin)
result=module.rank(payload['corpus'],payload['queries'],10)
print(json.dumps(result,ensure_ascii=False,allow_nan=False))
'''


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def frozen_data():
    sources = json.loads((ROOT / 'datasets/retrieval/retrieval_corpus_v3.json').read_text(encoding='utf-8'))
    questions = json.loads((ROOT / 'datasets/retrieval/retrieval_questions_v3.json').read_text(encoding='utf-8'))
    corpus = [{'id': f"{d['key']}:{i}", 'title': d['title'], 'section': c.get('section') or '',
               'content': c['content'], 'url': d['url'], 'page': c.get('page'), 'content_hash': sha(c['content'])}
              for d in sources for i, c in enumerate(d['chunks'])]
    data = {'corpus': corpus, 'questions': questions}
    return data, sha(json.dumps(data, ensure_ascii=False, sort_keys=True))


def configuration():
    data, fingerprint = frozen_data()
    return {'dataset': SUITE, 'dataset_version': '20260923', 'split': 'test', 'split_hash': fingerprint,
            'seeds': [42], 'code_revision': sha(BASELINE), 'command': 'registered:public-retrieval-v1',
            'environment': f'Python {platform.python_version()} / Windows native Codex sandbox / CPU',
            'parameters': {'suite': SUITE, 'top_k': 10, 'validation_questions': 20, 'test_questions': 40,
                           'evaluator_hash': sha(PREDICT + inspect.getsource(evaluate))}}


def metric_contract():
    return [{'name': name, 'unit': unit, 'direction': direction, 'target_mode': mode, 'target': target}
            for name, unit, direction, mode, target in (
                ('recall_at5', 'percent', 'higher', 'delta', 1),
                ('mrr', 'ratio', 'higher', 'delta', 0),
                ('latency_ms', 'ms/query including process start', 'lower', 'relative_percent', 0))]


def evaluate(workspace, data, cancelled, private_paths=(), runner=run_process):
    queries = data['questions']
    payload = json.dumps({'corpus': data['corpus'], 'queries': [q['query'] for q in queries]}, ensure_ascii=False)
    process = runner(sandbox_command(workspace, [sys.executable, '-X', 'utf8', '-I', '-B', '-c', PREDICT], private_paths),
                     workspace, timeout=30, cancelled=cancelled, stdin=payload)
    if process['termination'] != 'completed':
        return {'process': process, 'valid': False, 'error': process['termination']}
    try:
        predictions = json.loads(process['stdout'])
        if not isinstance(predictions, list) or len(predictions) != len(queries):
            raise ValueError('预测数量与冻结问题不一致')
        ids = {d['id'] for d in data['corpus']}
        rows = []
        for q, hits in zip(queries, predictions):
            if not isinstance(hits, list) or len(hits) > 10 or any(not isinstance(h, str) or h not in ids for h in hits) or len(hits) != len(set(hits)):
                raise ValueError('预测含重复、未知片段或无效格式')
            gold = {f"{g['document']}:{g['ordinal']}" for g in q['gold']}
            rows.append({'id': q['id'], 'split': q['split'], 'query': q['query'], 'gold': sorted(gold), 'hits': hits,
                         'recall_at5': len(set(hits[:5]) & gold) / len(gold) * 100 if gold else None,
                         'mrr': next((1 / (i + 1) for i, h in enumerate(hits) if h in gold), 0) if gold else None,
                         'unanswerable_has_hits': bool(hits) if not gold else None})
        groups = {}
        for split in ('dev', 'held_out'):
            selected = [r for r in rows if r['split'] == split and r['gold']]
            groups[split] = {'recall_at5': statistics.mean(r['recall_at5'] for r in selected),
                             'mrr': statistics.mean(r['mrr'] for r in selected),
                             'latency_ms': process['seconds'] * 1000 / len(queries)}
        return {'valid': True, 'process': process, 'metrics': groups['held_out'], 'validation': groups['dev'],
                'rows': rows, 'answerable_test': sum(r['split'] == 'held_out' and bool(r['gold']) for r in rows),
                'boundary': '历史语料；20开发/40测试。测试标签未传给候选；不是新增盲测。无答案项单列，不计可回答题召回。耗时包含进程启动。'}
    except (ValueError, TypeError, KeyError) as exc:
        return {'valid': False, 'process': process, 'error': str(exc)}
