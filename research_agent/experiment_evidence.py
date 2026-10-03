"""Optional registered experiment using host-owned, label-free evidence signals."""
import concurrent.futures
import inspect
import json
import threading
import time
import types

from . import experiment_feedback as feedback
from .evidence_rerank import fingerprint, rerank
from .experiment_process import ROOT
from .experiment_retrieval import dump, sha
from .models import model_from_env
from .usage import summarize_usage

SUITE = 'public-evidence-rerank-v1'
MODEL_PROFILE = 'sudocode-luna'
BASELINE = feedback.BASELINE
frozen_data = feedback.frozen_data
evaluate = feedback.evaluate
metric_contract = feedback.metric_contract
REQUEST_SECONDS = 120
WORKERS = 4


def model_identity(model):
    return {'profile': MODEL_PROFILE, 'name': model.name,
            'endpoint_hash': sha(model.base_url), 'temperature': model.temperature}


def configuration():
    config = feedback.configuration()
    config.update(dataset=SUITE, command='registered:' + SUITE)
    config['parameters'].update(suite=SUITE, reranker_hash=fingerprint(),
        signal_preparation_hash=sha(inspect.getsource(prepare_evidence)),
        reranker_model=model_identity(model_from_env(MODEL_PROFILE)),
        reranker_pool=40, reranker_seconds=REQUEST_SECONDS, reranker_workers=WORKERS,
        reranker_max_queries=config['parameters']['validation_questions'] + config['parameters']['test_questions'])
    return config


def prepare_data(data, cancelled=lambda: False):
    return prepare_evidence(feedback.prepare_data(data, cancelled), cancelled)


def prepare_evidence(data, cancelled=lambda: False):
    """Freeze query+original-text judgments before coding, never giving labels to LLM.

    Cache entries include failed calls; rerunning an experiment replays them instead
    of silently trying again until they succeed. Each query has one bounded logical
    model call (the existing transport can retry within its 120-second deadline).
    """
    if not 0 < len(data['questions']) <= 100:
        raise ValueError('evidence experiment requires 1–100 frozen questions')
    module = types.ModuleType('frozen_baseline')
    exec(compile(BASELINE, '<frozen-baseline>', 'exec'), module.__dict__)
    pools = module.rank(data['corpus'], data['signals'], 40)
    docs = {d['id']: d for d in data['corpus']}
    identity = model_identity(model_from_env(MODEL_PROFILE))
    source_hash = sha(json.dumps(data['corpus'], ensure_ascii=False, sort_keys=True))
    cache = ROOT / 'data/v4-evidence-signals'
    cache.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    query_locks = {q['query']: threading.Lock() for q in data['questions']}

    def prepare_locked(pair):
        q, pool = pair
        if cancelled(): raise InterruptedError('experiment cancelled')
        spec = {'query': q['query'], 'pool': pool, 'corpus_hash': source_hash,
                'model': identity, 'reranker_hash': fingerprint()}
        key = sha(json.dumps(spec, ensure_ascii=False, sort_keys=True))
        target = cache / (key + '.json')
        hit = target.is_file()
        if hit:
            saved = json.loads(target.read_text(encoding='utf-8'))
            if saved['spec'] != spec or saved['result_hash'] != sha(json.dumps(saved['result'], ensure_ascii=False, sort_keys=True)):
                raise ValueError('evidence feature cache hash mismatch')
            result = saved['result']
        else:
            # An in-flight synchronous response is already paid for. Cache it
            # before the outer cancellation check prevents coding/publishing.
            if cancelled(): raise InterruptedError('experiment cancelled')
            result = rerank(q['query'], [docs[k] for k in pool], model_from_env(MODEL_PROFILE),
                            seconds=REQUEST_SECONDS)
            saved = {'spec': spec, 'result': result,
                     'result_hash': sha(json.dumps(result, ensure_ascii=False, sort_keys=True))}
            dump(target.with_suffix('.tmp'), saved)
            target.with_suffix('.tmp').replace(target)
        return {'question_id': q['id'], 'cache_key': key, 'cache_hit': hit, **result}

    def prepare(pair):
        with query_locks[pair[0]['query']]:
            return prepare_locked(pair)

    # ponytail: four bounded synchronous requests, no extra queue/service dependency.
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        records = list(executor.map(prepare, zip(data['questions'], pools)))
    if cancelled(): raise InterruptedError('experiment cancelled')
    signals = [{**s, 'evidence_ranking': r['ranking'], 'evidence_status': r['status']}
               for s, r in zip(data['signals'], records)]
    usage = [u for r in records for u in r['usage_records']]
    current_usage = [u for r in records if not r['cache_hit'] for u in r['usage_records']]
    return {**data, 'signals': signals, 'evidence_records': records,
            'features': {**data['features'], 'dense_signals_hash': data['features']['signals_hash'],
                         'signals_hash': sha(json.dumps(signals, sort_keys=True)),
                         'evidence_rerank': {'model': identity, 'code_hash': fingerprint(), 'queries': len(records),
                             'fallbacks': sum(r['status'] != 'ok' for r in records),
                             'cache_hits': sum(r['cache_hit'] for r in records),
                             'preparation_seconds': time.perf_counter() - started,
                             'original_generation_seconds_sum': sum(r['seconds'] for r in records),
                             'original_usage': summarize_usage(usage), 'current_usage': summarize_usage(current_usage),
                             'boundary': 'Cached ranking time excludes original model calls; evidence spans prove provenance, not semantic correctness.'}}}
