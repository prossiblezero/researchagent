"""Measure the frozen offline QA judge, without running research tasks or tools."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent import load_dotenv
from research_agent.library import model_json
from research_agent.models import model_from_env
from research_agent.usage import summarize_usage
from evals.run_evidence_qa_acceptance import instrument, read, sanitize_full, write
from evals.run_intent_routing_eval import Budget, MAX_OUTPUT_TOKENS, sha, wilson

PANEL = ROOT / 'datasets/judge_calibration/v1.json'
LIMITS = {'seconds': 900, 'requests': 120, 'tokens': 800_000}
FIELDS = ('correct', 'complete', 'citation_sufficient', 'refused', 'unsupported_additions')
DIMENSIONS = {'correct': True, 'complete': True, 'citation_sufficient': True, 'unsupported_additions': False}


def decision(judgment):
    if not isinstance(judgment, dict) or not all(type(judgment.get(k)) is bool for k in FIELDS):
        raise ValueError('Judge must return all five boolean fields')
    return all(judgment[k] == wanted for k, wanted in DIMENSIONS.items())


def validate(panel):
    cases = panel['cases']
    ids = [c['id'] for c in cases]
    inputs = [json.dumps(c['input'], sort_keys=True, ensure_ascii=False) for c in cases]
    if len(cases) != 80 or len(set(ids)) != 80 or len(set(inputs)) != 80:
        raise ValueError('Expected 80 distinct frozen calibration cases')
    if any(type(c['expected_pass']) is not bool or not c['label_reason'] for c in cases):
        raise ValueError('Expected preassigned labels and reasons')
    if sum(c['expected_pass'] for c in cases) != 20:
        raise ValueError('Expected 20 positive and 60 negative controls')
    expected = {'question', 'reference', 'gold_originals', 'unanswerable', 'delivered_answer', 'cited_originals'}
    if any(set(c['input']) != expected for c in cases):
        raise ValueError('Only the existing judge payload may enter model requests')
    if sha(panel['judge_prompt'].encode()) != panel['judge_prompt_source']['prompt_sha256']:
        raise ValueError('Judge prompt identity changed')


def score(cases, rows):
    by_id = {r['id']: r for r in rows}
    if not cases or len(by_id) != len(rows) or set(by_id) - {c['id'] for c in cases}:
        raise ValueError('Empty panel, duplicate or unknown result')
    pairs = []
    for case in cases:
        row = by_id.get(case['id'], {})
        actual = None
        if not row.get('error') and row.get('judgment') is not None:
            try:
                actual = decision(row['judgment'])
            except ValueError:
                pass
        pairs.append((case, actual))
    positives = [(c, p) for c, p in pairs if c['expected_pass']]
    negatives = [(c, p) for c, p in pairs if not c['expected_pass']]
    tp = sum(p is True for c, p in positives)
    tn = sum(p is False for c, p in negatives)
    fp = sum(p is True for c, p in negatives)
    fn = sum(p is False for c, p in positives)
    pu = sum(p is None for c, p in positives)
    nu = sum(p is None for c, p in negatives)
    n = len(pairs)
    nvalid = n-pu-nu
    agreement = (tp+tn)/nvalid if nvalid else None
    chance = ((len(positives)-pu)*(tp+fp) + (len(negatives)-nu)*(tn+fn))/(nvalid*nvalid) if nvalid else None
    positive_f1 = 2*tp/(len(positives)+tp+fp) if len(positives)+tp+fp else 0
    negative_f1 = 2*tn/(len(negatives)+tn+fn) if len(negatives)+tn+fn else 0
    def subset(selected):
        return {'n': len(selected), 'correct': sum(p is c['expected_pass'] for c, p in selected),
                'errors': sum(p is None for c, p in selected)}
    return {'n': n, 'correct': tp+tn, 'accuracy': (tp+tn)/n, 'accuracy_wilson_95': wilson(tp+tn, n),
        'valid': nvalid, 'errors_or_missing': pu+nu, 'valid_only_agreement': agreement,
        'cohen_kappa_valid': (agreement-chance)/(1-chance) if chance is not None and chance < 1 else None,
        'macro_f1_with_failures': (positive_f1+negative_f1)/2,
        'confusion': {'positive_accepted': tp, 'positive_rejected': fn, 'positive_unknown': pu,
                      'negative_rejected': tn, 'negative_accepted': fp, 'negative_unknown': nu},
        'false_accept_rate_valid_negatives': fp/(len(negatives)-nu) if len(negatives)>nu else None,
        'false_accept_fraction_all_negatives': fp/len(negatives) if negatives else None,
        'false_accept_worst_case_with_unknown': (fp+nu)/len(negatives) if negatives else None,
        'false_reject_rate_valid_positives': fn/(len(positives)-pu) if len(positives)>pu else None,
        'by_kind': {k: subset([(c,p) for c,p in pairs if c['kind']==k]) for k in sorted({c['kind'] for c in cases})},
        'by_group': {k: subset([(c,p) for c,p in pairs if c['group']==k]) for k in sorted({c['group'] for c in cases})},
        'usage': summarize_usage([u for r in rows for u in r.get('usage', [])]),
        'failures': [{'id': c['id'], 'expected_pass': c['expected_pass'], 'actual_pass': p,
                      'error': by_id.get(c['id'], {}).get('error')} for c,p in pairs if p is not c['expected_pass']]}


def quality_metrics(rows):
    """Confirmed delivered quality over all original tasks; unknown fields stay visible."""
    keys = [(r['id'], r['arm']) for r in rows]
    if not rows or len(keys) != len(set(keys)):
        raise ValueError('Empty or duplicate project quality rows')
    result = {}
    for arm in sorted({r['arm'] for r in rows}):
        selected = [r for r in rows if r['arm']==arm]
        result[arm] = {'n': len(selected), 'product_completed': sum(r['product_completed'] for r in selected)}
        for phase in ('raw', 'reviewed'):
            judgments = []
            for row in selected:
                judgment = dict(row['raw_judgment'])
                if phase == 'reviewed' and row.get('adjudication'):
                    judgment.update(row['adjudication'])
                judgments.append((row, judgment))
            counts = {k: sum(r['product_completed'] and j.get('valid') is True and j.get(k) is wanted
                             for r,j in judgments) for k,wanted in DIMENSIONS.items()}
            unknown = {k: sum(r['product_completed'] and (not j.get('valid') or type(j.get(k)) is not bool)
                              for r,j in judgments) for k in DIMENSIONS}
            passed = sum(r['product_completed'] and j.get('valid') is True and
                         all(j.get(k) is wanted for k,wanted in DIMENSIONS.items()) for r,j in judgments)
            result[arm][phase] = {'confirmed_counts': counts,
                'confirmed_rates_all_tasks': {k:v/len(selected) for k,v in counts.items()},
                'unknown_delivered_counts': unknown, 'strict_passed': passed, 'strict_pass_rate': passed/len(selected),
                'valid_judgments': sum(j.get('valid') is True for r,j in judgments)}
    return result


def identity():
    files = ['evals/run_judge_calibration.py', 'evals/run_intent_routing_eval.py',
             'evals/run_evidence_qa_acceptance.py', 'research_agent/library.py',
             'research_agent/models.py', 'research_agent/usage.py', 'research_agent/contracts.py']
    return {name: sha((ROOT/name).read_bytes()) for name in files}


def evaluate_one(case, prompt, out, budget):
    folder = out/'cases'/case['id']
    folder.mkdir(parents=True, exist_ok=False)
    model = model_from_env('default')
    model.max_tokens = MAX_OUTPUT_TOKENS
    model.usage_purpose = 'judge_calibration'
    model.request_deadline = min(budget.deadline, time.monotonic()+90)
    instrument(model, folder, {'max_model_attempts': 3, 'max_case_seconds': 90, 'max_reported_tokens': LIMITS['tokens']})
    recorded = model._complete
    def bounded(messages, tools):
        reservation = budget.reserve(messages, tools)
        try:
            return recorded(messages, tools)
        finally:
            budget.finish(reservation, model.last_usage)
    model._complete = bounded
    start = time.perf_counter()
    judgment, error = None, None
    try:
        judgment = model_json(model, prompt, case['input'])
        decision(judgment)
    except Exception as exc:
        error = sanitize_full(type(exc).__name__+': '+str(exc), model.api_key)
    requests = [read(p) for p in sorted((folder/'requests').glob('*.json'))]
    row = {'id': case['id'], 'judgment': judgment, 'error': error, 'seconds': time.perf_counter()-start,
           'attempts': len(requests), 'usage': [{**(r.get('usage') or {}), 'error': r.get('error')} for r in requests]}
    write(folder/'result.json', row)
    return row


def run(out):
    panel = read(PANEL)
    validate(panel)
    out.mkdir(parents=True, exist_ok=False)
    load_dotenv(ROOT/'.env')
    model = model_from_env('default')
    if model.name != 'deepseek-v4-1-flash' or model.temperature != 0:
        raise ValueError('Calibration requires the saved memory-evaluation model and temperature')
    protocol = {'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'model': model.name,
        'temperature': model.temperature, 'endpoint_host': urlsplit(model.base_url).hostname,
        'timeout_seconds': model.timeout, 'max_output_tokens': MAX_OUTPUT_TOKENS,
        'case_seconds': 90, 'workers': 4, 'limits': LIMITS, 'panel_sha256': sha(PANEL.read_bytes()),
        'judge_prompt_sha256': sha(panel['judge_prompt'].encode()), 'source_files': identity(),
        'scoring': panel['scoring'], 'transmission': 'Only frozen public-source passages, synthetic questions/answers and reference facts; no expected pass labels, IDs, label reasons, private chats or credentials in message content.'}
    write(out/'protocol.json', protocol)
    write(out/'panel.json', panel)
    budget = Budget(LIMITS)
    def worker(case):
        row = evaluate_one(case, panel['judge_prompt'], out, budget)
        print(json.dumps({'id': row['id'], 'error': row['error'], 'attempts': row['attempts']}, ensure_ascii=True), flush=True)
        return row
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(worker, panel['cases']))
    write(out/'results.json', rows)
    metrics = score(panel['cases'], rows)
    metrics['budget_accounting'] = budget.snapshot()
    metrics['source_unchanged'] = identity()==protocol['source_files'] and sha(PANEL.read_bytes())==protocol['panel_sha256']
    write(out/'metrics.json', metrics)
    if not metrics['source_unchanged']:
        raise RuntimeError('Evaluation source changed; do not use as a frozen run')
    print(json.dumps({k:metrics[k] for k in ('n','correct','accuracy','errors_or_missing','confusion','usage')}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    run(parser.parse_args().out)
