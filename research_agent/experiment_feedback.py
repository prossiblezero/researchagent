"""Development feedback for a frozen FTS5/BGE-M3 fusion experiment.

Only development labels enter the coding workspace. The host scores the final
candidate; local developer-tool output is diagnostic, never accepted as a result.
"""
import inspect
import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from .experiment_process import ROOT, run_process, sandbox_command
from .experiment_retrieval import dump, sha
from .experiment_dataset import frozen_data, VERSION
from .retrieval import DenseIndex, REVISION, lexical, model_identity

SUITE = 'public-retrieval-feedback-v2'

BASELINE = '''import sqlite3
from contextlib import closing
import re

{lexical}

def rank(corpus, queries, top_k=10):
    """Existing FTS5 weights and RRF k=60 over frozen BGE-M3 parent hits."""
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE VIRTUAL TABLE chunks USING fts5(id UNINDEXED,title,section,body)')
        db.executemany('INSERT INTO chunks VALUES(?,?,?,?)', [(d['id'],lexical(d['title']),lexical(d['section']),lexical(d['content'])) for d in corpus])
        result=[]
        for query in queries:
            terms=list(dict.fromkeys(lexical(query['text']).split()))[:64]
            expression=' OR '.join('"'+t+'"' for t in terms)
            sparse=[r[0] for r in db.execute('SELECT id FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks,0,4,2,1) LIMIT 40', (expression,))] if expression else []
            dense=[hit['id'] for hit in query['dense']]
            scores={}
            for hits in (sparse,dense):
                for position,key in enumerate(hits,1):scores[key]=scores.get(key,0)+1/(60+position)
            result.append(sorted(scores,key=lambda key:(-scores[key],key))[:top_k])
        return result
'''.replace('{lexical}', inspect.getsource(lexical))


def score(corpus, questions, predictions):
    if not isinstance(predictions, list) or len(predictions) != len(questions):
        raise ValueError('预测数量与冻结问题不一致')
    ids = {d['id'] for d in corpus}
    rows = []
    for q, hits in zip(questions, predictions):
        if not isinstance(hits, list) or len(hits) > 10 or any(not isinstance(h, str) or h not in ids for h in hits) or len(set(hits)) != len(hits):
            raise ValueError('预测含重复、未知片段或无效格式')
        gold = {f"{g['document']}:{g['ordinal']}" for g in q['gold']}
        rows.append({'id': q['id'], 'split': q['split'], 'category': q['category'], 'query': q['query'],
                     'gold': sorted(gold), 'hits': hits,
                     'recall_at5': len(set(hits[:5]) & gold) / len(gold) * 100 if gold else None,
                     'mrr': next((1 / (i + 1) for i, hit in enumerate(hits) if hit in gold), 0) if gold else None})
    return rows


def summarize(rows):
    answerable = [r for r in rows if r['gold']]
    return {'recall_at5': statistics.mean(r['recall_at5'] for r in answerable) if answerable else None,
            'mrr': statistics.mean(r['mrr'] for r in answerable) if answerable else None,
            'zero_recall': sum(r['recall_at5'] == 0 for r in answerable), 'answerable': len(answerable)}


def development_snapshot(label, result, code_hash=None):
    """Return the small, label-safe record used by the host-side selector.

    ``result`` is produced by the host evaluator.  Keeping only validation
    metrics here makes the selection decision auditable without copying test
    rows into the coding workspace.
    """
    metrics = result.get('validation') if isinstance(result, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    return {'label': str(label), 'code_hash': code_hash,
            'metrics': {name: metrics.get(name) for name in ('recall_at5', 'mrr')}}


def select_best_development(baseline, trials):
    """Select a non-regressing development candidate, with baseline fallback.

    The baseline is always the initial best.  A trial must be no worse on both
    development Recall@5 and MRR before it can replace the current best.  The
    host owns this decision; model-reported metrics are never consulted.
    """
    if not isinstance(baseline, dict):
        raise ValueError('开发基线快照无效')
    base_metrics = baseline.get('metrics') or {}
    try:
        base_recall = float(base_metrics['recall_at5'])
        base_mrr = float(base_metrics['mrr'])
    except (KeyError, TypeError, ValueError):
        raise ValueError('开发基线缺少 Recall@5 或 MRR')
    if not math.isfinite(base_recall) or not math.isfinite(base_mrr):
        raise ValueError('开发基线指标无效')
    best = dict(baseline)
    accepted, rejected = [], []
    for raw in trials or []:
        trial = {key: raw.get(key) for key in ('label', 'code_hash', 'metrics')} if isinstance(raw, dict) else {'label': 'unknown', 'metrics': {}}
        metrics = trial.get('metrics') or {}
        try:
            recall, mrr = float(metrics['recall_at5']), float(metrics['mrr'])
            valid = math.isfinite(recall) and math.isfinite(mrr)
        except (KeyError, TypeError, ValueError):
            recall = mrr = None
            valid = False
        if valid and recall >= base_recall and mrr >= base_mrr:
            accepted.append(trial)
            best_metrics = best.get('metrics') or {}
            if (recall, mrr) > (float(best_metrics.get('recall_at5', -math.inf)),
                                float(best_metrics.get('mrr', -math.inf))):
                best = trial
        else:
            trial['rejection'] = 'development regression or invalid metrics'
            rejected.append(trial)
    return {
        'selected': best.get('label', 'baseline'),
        'baseline': {'recall_at5': base_recall, 'mrr': base_mrr},
        'accepted': accepted,
        'rejected': rejected,
        'rule': 'baseline initial best; candidate Recall@5 and MRR must both be >= baseline; select max Recall@5 then MRR',
    }


# This immutable script helps the coding agent inspect development failures.
# Gold test labels and final grading code never enter its workspace.
DEV_EVAL = '''import hashlib,json,statistics,sys,types
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
{score}
{summarize}
code=Path('ranker.py').read_text(encoding='utf-8')
code_hash=hashlib.sha256(code.encode('utf-8')).hexdigest()
slots=[Path('candidate-first.json'),Path('candidate-revised.json')]
snapshots=[json.loads(path.read_text(encoding='utf-8')) for path in slots]
if not any(snapshot.get('code_hash')==code_hash for snapshot in snapshots):
    empty=next((path for path,snapshot in zip(slots,snapshots) if not snapshot),None)
    if empty is None:raise ValueError('Only first candidate and one revision are allowed')
    empty.write_text(json.dumps({'code_hash':code_hash,'code':code},ensure_ascii=False),encoding='utf-8')
module=types.ModuleType('candidate')
exec(compile(code,'ranker.py','exec'),module.__dict__)
corpus=json.loads(Path('corpus.json').read_text(encoding='utf-8'))
questions=json.loads(Path('dev.json').read_text(encoding='utf-8'))
queries=json.loads(Path('dev-inputs.json').read_text(encoding='utf-8'))
rows=score(corpus,questions,module.rank(corpus,queries,10))
baseline=json.loads(Path('dev-feedback.json').read_text(encoding='utf-8'))
before={row['id']:row for row in baseline.get('rows',[])}
changes=[{'id':row['id'],'query':row['query'],'before_recall':before[row['id']]['recall_at5'],
          'after_recall':row['recall_at5'],'before_mrr':before[row['id']]['mrr'],'after_mrr':row['mrr']}
         for row in rows if row['id'] in before and
         (row['recall_at5'],row['mrr']) != (before[row['id']]['recall_at5'],before[row['id']]['mrr'])]
print(json.dumps({'summary':summarize(rows),'changes':changes,
                 'remaining_failures':[row for row in rows if row['recall_at5'] is not None and row['recall_at5']<100],
                 **({'rows':rows} if '--full' in sys.argv else {})},ensure_ascii=False))
'''.replace('{score}', inspect.getsource(score)).replace('{summarize}', inspect.getsource(summarize)).replace('{{', '{').replace('}}', '}')

# Algorithm time is instrumented by this wrapper and is diagnostic only. Wall
# time remains host measured; neither can authorize a candidate on its own.
PREDICT = '''import copy,importlib.util,json,sys,time,statistics
clock=time.perf_counter
payload=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('candidate','ranker.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
samples=[];predictions=[]
for _ in range(3):
    start=clock()
    result=module.rank(payload['corpus'],payload['queries'],10)
    samples.append((clock()-start)*1000)
    predictions.append(copy.deepcopy(result))
print(json.dumps({'predictions':predictions,'rank_batch_ms':samples},ensure_ascii=False,allow_nan=False))
'''


def configuration():
    data, fingerprint = frozen_data()
    return {'dataset': SUITE, 'dataset_version': VERSION, 'split': 'test', 'split_hash': fingerprint,
            'seeds': [42], 'code_revision': sha(BASELINE), 'command': 'registered:' + SUITE,
            'environment': f'Python {platform.python_version()} / Windows native Codex sandbox / CPU',
            'parameters': {'suite': SUITE, 'top_k': 10, 'validation_questions': data['dataset_info']['dev_questions'],
                           'test_questions': data['dataset_info']['final_questions'],
                           'historical_regression_questions': data['dataset_info']['historical_questions'],
                           'encoder_revision': REVISION, 'model_identity': model_identity(Path(os.getenv('BGE_MODEL_PATH', str(ROOT / 'data/models/bge-m3')))),
                           'feature_code_hash': feature_fingerprint(),
                           'evaluator_hash': sha(PREDICT + inspect.getsource(score) + inspect.getsource(summarize) + inspect.getsource(evaluate)),
                           'development_tool_hash': sha(DEV_EVAL),
                           'selection_hash': sha(inspect.getsource(select_best_development)), 'timing_repeats': 3}}


def metric_contract():
    # Runtime is reported separately: millisecond process-start noise must not
    # reject a quality improvement. The existing 30-second process cap remains.
    return [{'name': name, 'unit': unit, 'direction': 'higher', 'target_mode': 'delta', 'target': target}
            for name, unit, target in [('recall_at5', 'percent', 1), ('mrr', 'ratio', 0)]]


def feature_fingerprint():
    return sha(REVISION + inspect.getsource(DenseIndex.segments) + inspect.getsource(DenseIndex.encode) + inspect.getsource(prepare_data) +
               json.dumps(model_identity(Path(os.getenv('BGE_MODEL_PATH', str(ROOT / 'data/models/bge-m3')))), sort_keys=True))


def prepare_data(data, cancelled=lambda: False):
    """Frozen label-free semantic signals, encoded once using existing weights."""
    import numpy as np
    model = model_identity(Path(os.getenv('BGE_MODEL_PATH', str(ROOT / 'data/models/bge-m3'))))
    key = sha(json.dumps({'corpus': data['corpus'], 'queries': [q['query'] for q in data['questions']],
                         'code': feature_fingerprint(), 'model': model}, ensure_ascii=False, sort_keys=True))
    cache = ROOT / 'data' / 'v4-features' / key
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / 'signals.json'
    start = time.perf_counter()
    if target.is_file():
        record = json.loads(target.read_text(encoding='utf-8'))
        if (record['key'] != key or record.get('model_identity') != model or
                record['signals_hash'] != sha(json.dumps(record['signals'], sort_keys=True))):
            raise ValueError('冻结语义信号校验失败')
        signals = record['signals']
        cache_hit = True
    else:
        model_path = Path(os.getenv('BGE_MODEL_PATH', str(ROOT / 'data/models/bge-m3')))
        index = DenseIndex(cache, model_path=model_path)
        segments, parents = [], []
        for doc in data['corpus']:
            if cancelled(): raise RuntimeError('实验已停止')
            for _, _, text in index.segments(doc['content']):
                segments.append(text); parents.append(doc['id'])
        vectors = []
        for offset in range(0, len(segments), 8):
            if cancelled(): raise RuntimeError('实验已停止')
            vectors.extend(index.encode(segments[offset:offset + 8]))
        matrix = np.asarray(vectors, dtype=np.float32)
        signals = []
        for q in data['questions']:
            if cancelled(): raise RuntimeError('实验已停止')
            similarities = matrix @ np.asarray(index.encode([q['query']])[0], dtype=np.float32)
            best = {}
            for parent, value in zip(parents, similarities):best[parent] = max(best.get(parent, -1.), float(value))
            signals.append({'text': q['query'], 'dense': [{'id': key, 'score': best[key]} for key in sorted(best, key=lambda k: (-best[k], k))[:40]]})
        record = {'key': key, 'encoder_revision': REVISION, 'model_identity': model, 'segments': len(segments), 'signals': signals,
                  'signals_hash': sha(json.dumps(signals, sort_keys=True)), 'generation_seconds': time.perf_counter() - start}
        dump(cache / 'signals.tmp', record); (cache / 'signals.tmp').replace(target)
        cache_hit = False
    return {**data, 'signals': signals, 'features': {k: v for k, v in record.items() if k != 'signals'} | {
        'cache_hit': cache_hit, 'preparation_seconds': time.perf_counter() - start}}


def development_files(data, baseline):
    """Bounded source-linked feedback, strictly filtered to development rows."""
    docs = {d['id']: d for d in data['corpus']}
    signals = {q['id']: s for q, s in zip(data['questions'], data['signals']) if q['split'] == 'dev'}
    rows = [r for r in baseline['rows'] if r['split'] == 'dev']
    feedback = []
    corpus_terms = {key:set(lexical(' '.join(doc.get(k,'') for k in ('title','section','content'))).split()) for key,doc in docs.items()}
    for row in rows:
        failed = row['recall_at5'] is not None and row['recall_at5'] < 100
        dense = signals[row['id']]['dense']
        query_terms = set(list(dict.fromkeys(lexical(row['query']).split()))[:64])
        lexical_matches = sum(bool(query_terms & terms) for terms in corpus_terms.values())
        feedback.append({**row, 'missing_at5': sorted(set(row['gold']) - set(row['hits'][:5])),
                         'lexical_matching_chunks': lexical_matches,
                         'dense_gold_ranks': {key: next((i + 1 for i, hit in enumerate(dense) if hit['id'] == key), None) for key in row['gold']},
                          'dense_top5': dense[:5] if failed else [],
                         'expected_evidence': [{**{k: docs[key][k] for k in ('id', 'title', 'url', 'page')},
                                                 'excerpt': docs[key]['content'][:600 if failed else 80], 'full_text': 'corpus.json id=' + key}
                                               for key in row['gold']]})
    return {'dev-inputs.json': [signal for q, signal in zip(data['questions'], data['signals']) if q['split'] == 'dev'],
            'dev-feedback.json': {'summary': summarize(rows), 'rows': feedback,
                                  'boundary': 'Only development evidence. Final fact groups and rankings remain private; old 60 questions are a separate historical control.'}}


def evaluate(workspace, data, cancelled, private_paths=(), runner=run_process):
    payload = json.dumps({'corpus': data['corpus'], 'queries': data['signals']}, ensure_ascii=False)
    process = runner(sandbox_command(workspace, [sys.executable, '-X', 'utf8', '-I', '-B', '-c', PREDICT], private_paths),
                     workspace, timeout=30, cancelled=cancelled, stdin=payload)
    if process['termination'] != 'completed':
        return {'process': process, 'valid': False, 'error': process['termination']}
    try:
        result = json.loads(process['stdout']); predictions = result['predictions']; samples = result['rank_batch_ms']
        if not isinstance(predictions, list) or len(predictions) != 3 or predictions[1:] != predictions[:1] * 2:
            raise ValueError('重复测量输出不一致')
        if not isinstance(samples, list) or len(samples) != 3 or any(type(n) not in (int, float) or not math.isfinite(n) or n < 0 for n in samples):
            raise ValueError('计时记录无效')
        rows = score(data['corpus'], data['questions'], predictions[0])
        validation = summarize([r for r in rows if r['split'] == 'dev'])
        test_rows = [r for r in rows if r['split'] == 'held_out']
        test = summarize(test_rows) if test_rows else None
        return {'valid': True, 'process': process, 'rows': rows,
                'metrics': {k: test[k] for k in ('recall_at5', 'mrr')} if test else {}, 'validation': validation,
                'by_category': {category: summarize([r for r in rows if r['split'] == 'held_out' and r['category'] == category])
                                for category in sorted({r['category'] for r in rows if r['split'] == 'held_out' and r['gold']})},
                'timing': {'rank_ms_per_query': statistics.median(samples) / len(rows), 'rank_batch_ms': samples,
                           'process_wall_ms': process['seconds'] * 1000,
                           'startup_io_cleanup_ms_estimate': max(0., process['seconds'] * 1000 - sum(samples)),
                           'semantic_preparation': data['features'],
                           'boundary': 'Rank-call timing includes FTS index construction; instrumented diagnostics, not a promotion gate. Host wall time includes sandbox startup, IO and cleanup. Frozen embedding setup is separate.'},
                'answerable_test': test['answerable'] if test else 0, 'zero_recall_test': test['zero_recall'] if test else 0,
                'boundary': 'Frozen source-passage ranking with disjoint development/final fact groups; not the entire production RAG pipeline. Historical suites retain their original recorded split.'}
    except (ValueError, TypeError, KeyError, IndexError, statistics.StatisticsError) as exc:
        return {'valid': False, 'process': process, 'error': str(exc)}


def development_data(data):
    """The host remeasures candidates on this strict dev-only projection."""
    pairs = [(q, s) for q, s in zip(data['questions'], data['signals']) if q['split'] == 'dev']
    return {**data, 'questions': [q for q, _ in pairs], 'signals': [s for _, s in pairs]}
