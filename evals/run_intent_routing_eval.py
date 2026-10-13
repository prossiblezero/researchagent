"""Frozen, exact-label evaluation of the production seven-way intent router.

Only route_intent is called: no search, downloads, code execution or judge calls.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent import load_dotenv
from research_agent.models import model_from_env
from research_agent.usage import summarize_usage
from research_agent.workbench import INTENTS, ROUTER_PROMPT, OfflineRouter, route_intent
from evals.run_evidence_qa_acceptance import instrument, read, sanitize_full, write

PANEL = ROOT / 'datasets/intent_routing/v1.json'
LABELS = ('CHAT', 'RESEARCH', 'LOCAL_QA', 'DOWNLOAD', 'BRAINSTORM', 'EXPERIMENT', 'AUTO_RESEARCH')
BUDGETS = {
    'dev': {'seconds': 420, 'requests': 80, 'tokens': 300_000},
    'test': {'seconds': 1200, 'requests': 280, 'tokens': 1_500_000},
}
CASE_SECONDS = 60
MAX_OUTPUT_TOKENS = 4096
WORKERS = 4


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate(panel):
    if set(panel['intents']) != INTENTS or not panel['cases']:
        raise ValueError('Seven nonempty intent classes required')
    ids, groups, inputs, origins = set(), {}, set(), set()
    counts = {(s, label): 0 for s in BUDGETS for label in LABELS}
    for case in panel['cases']:
        key, split, label = case['id'], case['split'], case['expected_intent']
        if key in ids or split not in BUDGETS or label not in INTENTS:
            raise ValueError('Duplicate ID, invalid split or unknown label')
        ids.add(key)
        if groups.setdefault(case['group'], split) != split:
            raise ValueError('Group crosses development and test splits')
        history, space, scope = model_inputs(case)
        encoded = json.dumps([history, space, scope], sort_keys=True, ensure_ascii=False)
        if encoded in inputs:
            raise ValueError('Duplicate model input')
        inputs.add(encoded)
        if not history or history[-1]['role'] != 'user' or not case['rationale']:
            raise ValueError('Latest user turn and preassigned rationale required')
        for message in history:
            if set(message) != {'role', 'content'} or message['role'] not in {'user', 'assistant'} or not isinstance(message['content'], str) or not message['content'].strip():
                raise ValueError('Invalid public/synthetic history')
        if case['origin']['kind'] == 'MASSIVE':
            source_id = case['origin']['id']
            if source_id in origins:
                raise ValueError('Translated source ID repeated')
            origins.add(source_id)
        counts[(split, label)] += 1
    expected_test = {label: 30 for label in LABELS}
    expected_test.update(CHAT=34, AUTO_RESEARCH=26)  # Four missing-topic labels corrected by independent policy audit.
    if any(counts[('dev', label)] != 6 or counts[('test', label)] != expected_test[label] for label in LABELS):
        raise ValueError('Panel must have 42 development and 210 audited test cases')
    return counts


def model_inputs(case):
    # Gold labels, rationales, IDs, tags and source annotations never reach the model.
    return case['history'], case['space'], case['search_scope']


def wilson(correct, total):
    if not total:
        return None
    z = 1.959963984540054
    p = correct / total
    centre = (p + z*z/(2*total)) / (1 + z*z/total)
    radius = z * math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / (1 + z*z/total)
    return [max(0, centre-radius), min(1, centre+radius)]


def score(cases, rows):
    by_id = {row['id']: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) - {c['id'] for c in cases}:
        raise ValueError('Duplicate or unknown result ID')
    if not cases:
        raise ValueError('Cannot score an empty panel')
    pairs = []
    for case in cases:
        row = by_id.get(case['id'], {})
        predicted = (row.get('response') or {}).get('intent')
        if row.get('error') or predicted not in INTENTS:
            predicted = 'ERROR'
        pairs.append((case, predicted))
    confusion = {label: {pred: 0 for pred in (*LABELS, 'ERROR')} for label in LABELS}
    for case, predicted in pairs:
        confusion[case['expected_intent']][predicted] += 1
    per_class = {}
    for label in LABELS:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        predicted = sum(row[label] for row in confusion.values())
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        per_class[label] = {'n': support, 'correct': tp, 'precision': precision, 'recall': recall,
                            'f1': 2*tp/(support+predicted) if support+predicted else 0.0}
    def aggregate(selected):
        n = len(selected)
        correct = sum(c['expected_intent'] == pred for c, pred in selected)
        return {'n': n, 'correct': correct, 'accuracy': correct/n if n else None}
    totals = aggregate(pairs)
    auto_negatives = [(c, p) for c, p in pairs if c['expected_intent'] != 'AUTO_RESEARCH']
    false_auto = sum(p == 'AUTO_RESEARCH' for c, p in auto_negatives)
    times = sorted(r['seconds'] for r in rows if r.get('attempts', 0))
    return {**totals, 'accuracy_wilson_95': wilson(totals['correct'], totals['n']),
        'macro_f1': statistics.mean(v['f1'] for v in per_class.values()),
        'valid_routes': sum(p != 'ERROR' for c, p in pairs),
        'errors_or_missing': sum(p == 'ERROR' for c, p in pairs),
        'auto_research_false_positives': false_auto, 'auto_research_negative_cases': len(auto_negatives),
        'auto_research_false_positive_rate': false_auto/len(auto_negatives) if auto_negatives else None,
        'per_class': per_class, 'confusion': confusion,
        'strata': {kind: aggregate([(c, p) for c, p in pairs if c['origin']['kind'] == kind]) for kind in ('MASSIVE', 'authored')},
        'tags': {tag: aggregate([(c, p) for c, p in pairs if tag in c['tags']]) for tag in sorted({t for c in cases for t in c['tags']})},
        'latency_seconds': {'median': statistics.median(times) if times else None,
                            'p95': times[math.ceil(.95*len(times))-1] if times else None},
        'usage': summarize_usage([u for r in rows for u in r.get('usage', [])]),
        'failures': [{'id': c['id'], 'expected': c['expected_intent'], 'predicted': p,
                      'error': by_id.get(c['id'], {}).get('error', 'missing result' if c['id'] not in by_id else None)}
                     for c, p in pairs if c['expected_intent'] != p]}


class Budget:
    """One process-wide admission budget; reserve conservative bytes + output tokens.

    Missing usage consumes its reservation. This is an accounting bound, not a price quote.
    """
    def __init__(self, limits):
        self.limits = limits
        self.deadline = time.monotonic() + limits['seconds']
        self.lock = threading.Lock()
        self.requests = self.accounted = self.pending = self.unknown = 0

    def reserve(self, messages, tools):
        amount = len(json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False).encode('utf-8')) + MAX_OUTPUT_TOKENS + 1024
        with self.lock:
            if time.monotonic() >= self.deadline or self.requests >= self.limits['requests'] or self.accounted + self.pending + amount > self.limits['tokens']:
                raise RuntimeError('Frozen run request/time/token budget exhausted')
            self.requests += 1
            self.pending += amount
        return amount

    def finish(self, reserved, usage):
        used = (usage or {}).get('total_tokens')
        with self.lock:
            self.pending -= reserved
            if used is None:
                self.unknown += 1
            self.accounted += used if used is not None else reserved

    def snapshot(self):
        with self.lock:
            return {'attempts_admitted': self.requests, 'accounted_tokens_including_unknown_reservations': self.accounted,
                    'pending_reserved_tokens': self.pending, 'unknown_usage_attempts': self.unknown}


def code_manifest():
    paths = [*sorted((ROOT/'research_agent').glob('*.py')), Path(__file__), ROOT/'evals/run_evidence_qa_acceptance.py']
    return {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p.read_bytes()) for p in paths}


def evaluate_one(case, out, model_id, budget):
    folder = out / 'cases' / case['id']
    folder.mkdir(parents=True, exist_ok=False)
    model = model_from_env(model_id)
    model.usage_purpose = 'intent_routing_evaluation'
    model.max_tokens = MAX_OUTPUT_TOKENS
    model.request_deadline = min(budget.deadline, time.monotonic()+CASE_SECONDS)
    model = instrument(model, folder, {'max_model_attempts': 6, 'max_case_seconds': CASE_SECONDS,
                                       'max_reported_tokens': budget.limits['tokens']})
    recorded = model._complete
    def bounded(messages, tools):
        reserved = budget.reserve(messages, tools)
        try:
            return recorded(messages, tools)
        finally:
            budget.finish(reserved, model.last_usage)
    model._complete = bounded
    started = time.perf_counter()
    response, error = None, None
    try:
        response = route_intent(model, *model_inputs(case))
    except Exception as exc:
        error = sanitize_full(type(exc).__name__+': '+str(exc), model.api_key)
    requests = [read(p) for p in sorted((folder/'requests').glob('*.json'))]
    row = {'id': case['id'], 'response': response, 'error': error, 'seconds': time.perf_counter()-started,
           'attempts': len(requests), 'usage': [{**(r.get('usage') or {}), 'error': r.get('error')} for r in requests]}
    write(folder/'result.json', row)
    return row


def report(out, protocol, metrics, baseline):
    def pct(v):
        return f'{100*v:.2f}%'
    lines = ['# Intent routing evaluation', '', f"Split: {protocol['split']}; model: {protocol['model']}; dataset SHA256: `{protocol['panel_sha256']}`.", '',
        f"Exact accuracy: **{metrics['correct']}/{metrics['n']} ({pct(metrics['accuracy'])})**; macro-F1: **{pct(metrics['macro_f1'])}**.",
        f"Valid routes: {metrics['valid_routes']}/{metrics['n']}; invalid/API/budget/missing failures: {metrics['errors_or_missing']}.",
        f"AUTO_RESEARCH false positives: {metrics['auto_research_false_positives']}/{metrics['auto_research_negative_cases']}.", '',
        '|Intent|Correct / total|Precision|Recall|F1|', '|---|---:|---:|---:|---:|']
    for label, row in metrics['per_class'].items():
        lines.append(f"|{label}|{row['correct']}/{row['n']}|{pct(row['precision'])}|{pct(row['recall'])}|{pct(row['f1'])}|")
    lines += ['', f"Keyword-only OfflineRouter demo baseline: {baseline['correct']}/{baseline['n']} ({pct(baseline['accuracy'])}). It lacks an AUTO_RESEARCH route and is not a competing production system.", '',
        'All selected cases count in the denominator, including failed and missing results. Labels/rationales are not sent to the classifier. No LLM judge is used.',
        'This is a MASSIVE-subset + authored project-taxonomy evaluation, not official MASSIVE accuracy or end-to-end workflow success. Authored labels were self-reviewed by the implementation agent, not independently human-annotated.',
        'The Wilson interval in metrics.json assumes independent cases; authored cases can share wording and scenario patterns. Treat it as descriptive, not a population guarantee.', '',
        'Failures:']
    lines += [f"- {r['id']}: {r['expected']} -> {r['predicted']} ({r['error'] or 'label mismatch'})" for r in metrics['failures']] or ['- None']
    (out/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def run(out, split, model_id):
    panel = read(PANEL)
    validate(panel)
    # Existing runs are immutable; never silently retry a bad case or choose best-of-N.
    out.mkdir(parents=True, exist_ok=False)
    load_dotenv(ROOT/'.env')
    model = model_from_env(model_id)
    protocol = {'schema_version': 1, 'split': split, 'model_id': model_id, 'model': model.name,
        'endpoint_host': urlsplit(model.base_url).hostname, 'temperature': model.temperature,
        'max_output_tokens': MAX_OUTPUT_TOKENS, 'workers': WORKERS, 'case_seconds': CASE_SECONDS,
        'budget': BUDGETS[split], 'panel_sha256': sha(PANEL.read_bytes()), 'router_prompt_sha256': sha(ROUTER_PROMPT.encode()),
        'source_files': code_manifest(), 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'scoring': 'Exact label, all cases including API/schema/budget failures in denominator; macro-F1 over fixed 7 labels.',
        'transmission': 'Production router system prompt + only public/synthetic histories and workspace metadata; no gold labels, private chats, files or credentials in message content.'}
    write(out/'protocol.json', protocol)
    write(out/'panel.json', panel)
    cases = [c for c in panel['cases'] if c['split'] == split]
    baseline_rows = []
    for case in cases:
        response, error = None, None
        try:
            response = route_intent(OfflineRouter(), *model_inputs(case))
        except Exception as exc:
            error = str(exc)
        baseline_rows.append({'id': case['id'], 'response': response, 'error': error})
    baseline = score(cases, baseline_rows)
    write(out/'offline-demo-results.json', baseline_rows)
    write(out/'offline-demo-metrics.json', baseline)
    budget, rows = Budget(BUDGETS[split]), []
    def worker(case):
        try:
            row = evaluate_one(case, out, model_id, budget)
        except Exception as exc:
            row = {'id': case['id'], 'response': None, 'error': sanitize_full(type(exc).__name__+': '+str(exc), model.api_key), 'attempts': 0, 'seconds': 0, 'usage': []}
            write(out/'cases'/case['id']/'result.json', row)
        print(json.dumps({'id': row['id'], 'intent': (row['response'] or {}).get('intent'), 'error': row['error']}, ensure_ascii=True), flush=True)
        return row
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for row in pool.map(worker, cases):
            rows.append(row)
    # Each case is durable already; avoid repeatedly replacing an open Windows summary.
    write(out/'results.json', rows)
    metrics = score(cases, rows)
    metrics['budget_accounting'] = budget.snapshot()
    metrics['source_unchanged_during_run'] = protocol['source_files'] == code_manifest() and protocol['panel_sha256'] == sha(PANEL.read_bytes())
    write(out/'metrics.json', metrics)
    report(out, protocol, metrics, baseline)
    if not metrics['source_unchanged_during_run']:
        raise RuntimeError('Source or panel changed during run: do not use as a frozen evaluation')
    print(json.dumps({k: metrics[k] for k in ('n','correct','accuracy','macro_f1','errors_or_missing','usage')}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=tuple(BUDGETS), required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--model', default='sudocode-luna')
    args = parser.parse_args()
    run(args.out, args.split, args.model)
