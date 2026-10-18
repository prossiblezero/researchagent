"""Immutable v2/v3 router evaluation; no routed task is ever executed."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent import load_dotenv
from research_agent.decision_router import LABELS, OptionScorer, DecisionRouter, prompt_hash, state_text, weight_identity, validate_config
from research_agent.models import model_from_env
from research_agent.workbench import route_intent
from evals.build_intent_routing_v2 import read, write, sha, text, verify_frozen
from evals.calibrate_decision_router import distribution_metrics, diagrams, paired_comparison
from evals.run_intent_routing_eval import Budget, score, model_inputs, MAX_OUTPUT_TOKENS, CASE_SECONDS, BUDGETS as V1_BUDGETS
from evals.run_evidence_qa_acceptance import instrument, sanitize_full

LIMITS = {'seconds': 900, 'requests': 200, 'tokens': 1_000_000}
V3_GROUPS = {'A', 'D', 'E2', 'C1-4B', 'E2-4B'}


def group_method(group):
    return {'E2': 'E', 'C1-4B': 'C1', 'E2-4B': 'E'}.get(group, group)


def hardware():
    import torch
    result = {'platform': platform.platform(), 'python': platform.python_version(), 'torch': torch.__version__,
              'cuda': torch.version.cuda, 'cuda_available': torch.cuda.is_available()}
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        result.update(gpu=props.name, gpu_memory_bytes=props.total_memory, bf16=torch.cuda.is_bf16_supported())
    return result


def source_manifest():
    paths = sorted((ROOT/'research_agent').glob('*.py')) + sorted((ROOT/'evals').glob('*decision_router*.py'))
    paths += [ROOT/'evals/build_intent_routing_v2.py', ROOT/'evals/build_intent_routing_v3.py', ROOT/'evals/run_intent_routing_eval.py',
              ROOT/'evals/run_evidence_qa_acceptance.py', ROOT/'requirements-router.txt']
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in paths}


def percentile(values, p):
    values = sorted(values)
    return values[max(0, math.ceil(p*len(values))-1)] if values else None


def summarize(cases, rows, temperature=1., *, cascade=False):
    result = score(cases, rows)
    result['calibration'] = distribution_metrics(cases, rows, temperature)
    result['non_chat_predicted_chat'] = sum(c['expected_intent'] != 'CHAT' and
        (r.get('response') or {}).get('intent') == 'CHAT' and not r.get('error')
        for c in cases for r in rows if r['id'] == c['id'])
    for key in ('intent_seconds', 'route_seconds'):
        values = [r[key] for r in rows if r.get(key) is not None]
        result[key] = {'n': len(values), 'p50': percentile(values, .5), 'p95': percentile(values, .95)}
    indexed = {r['id']: r for r in rows}
    fallback = [c for c in cases if indexed.get(c['id'], {}).get('decision', {}).get('fallback')]
    observed = [c for c in cases if 'decision' in indexed.get(c['id'], {})]
    accepted = [c for c in observed if c not in fallback]
    if cascade or observed:
        result['cascade'] = {'fallback_rate': len(fallback)/len(observed) if observed else None,
            'decision_observations': len(observed), 'unobserved_cases': len(cases)-len(observed),
            'fallback': score(fallback, [indexed[c['id']] for c in fallback]) if fallback else None,
            'accepted': score(accepted, [indexed[c['id']] for c in accepted if c['id'] in indexed]) if accepted else None,
            'field_completion_count': sum(r.get('decision', {}).get('field_completion', False) for r in rows)}
    subsets = {'keyword_non_execution': [c for c in cases if any(k in text(c) for k in ('实验', '代码')) and c['expected_intent'] not in {'EXPERIMENT', 'AUTO_RESEARCH'}],
               'execution_without_keywords': [c for c in cases if not any(k in text(c) for k in ('实验', '代码')) and c['expected_intent'] in {'EXPERIMENT', 'AUTO_RESEARCH'}]}
    result['keyword_shortcuts'] = {name: score(selected, [indexed[c['id']] for c in selected if c['id'] in indexed])
                                  if selected else None for name, selected in subsets.items()}
    return result


def guarded_remote(case, out, model_id, budget, router=None):
    folder = out/'cases'/case['id']
    folder.mkdir(parents=True, exist_ok=False)
    start, events, model = time.perf_counter(), [], None
    response, error = None, None
    try:
        model = model_from_env(model_id)
        model.usage_purpose = 'decision_router_evaluation'
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
        response = (router.route(model, *model_inputs(case), on_decision=events.append) if router
                    else route_intent(model, *model_inputs(case)))
    except Exception as exc:
        error = sanitize_full(type(exc).__name__ + ': ' + str(exc), getattr(model, 'api_key', ''))
    elapsed = time.perf_counter()-start
    requests = [read(p) for p in sorted((folder/'requests').glob('*.json'))]
    row = {'id': case['id'], 'response': response, 'error': error, 'seconds': elapsed, 'route_seconds': elapsed,
           'intent_seconds': events[-1].get('intent_seconds', elapsed) if events else elapsed,
           'attempts': len(requests), 'usage': [{**(r.get('usage') or {}), 'error': r.get('error')} for r in requests]}
    if events:
        row['decision'] = events[-1]
        # Local confidence describes the local choice, not the remote final label.
        # Keep it separate; cascade final calibration is not estimable from this.
    write(folder/'result.json', row)
    return row


def failed_remote_receipts(case, out, error):
    """Retain logged requests when a worker fails outside its model-call guard."""
    paths = sorted((out/'cases'/case['id']/'requests').glob('*.json'))
    usage = []
    for path in paths:
        try:
            receipt = read(path)
            usage.append({**(receipt.get('usage') or {}), 'error': receipt.get('error')})
        except (OSError, ValueError):
            usage.append({'error': 'unreadable request receipt'})
    return {'id': case['id'], 'response': None, 'error': sanitize_full(type(error).__name__+': '+str(error)),
            'seconds': 0, 'attempts': len(paths), 'usage': usage, 'collection_failed': True}


class EmbeddingClassifier:
    def __init__(self, panel, encoder_path, device, max_chars=6000):
        from sentence_transformers import SentenceTransformer
        from sklearn.linear_model import LogisticRegression
        started = time.perf_counter()
        self.encoder = SentenceTransformer(str(encoder_path), device=None if device == 'auto' else device, local_files_only=True)
        self.max_chars = max_chars
        self.cold_start_seconds = time.perf_counter()-started
        started = time.perf_counter()
        train = [c for c in panel['cases'] if c['split'] == 'train_v2']
        vectors = self.encoder.encode([self.input_text(c) for c in train], batch_size=16, normalize_embeddings=True, show_progress_bar=True)
        self.classifier = LogisticRegression(C=1., max_iter=1000, random_state=17)
        self.classifier.fit(vectors, [c['expected_intent'] for c in train])
        self.classifier_fit_seconds = time.perf_counter()-started

    def input_text(self, case):
        history, space, scope = model_inputs(case)
        history = list(history)
        while True:
            value = state_text(history, space, scope, max_chars=self.max_chars)
            if len(self.encoder.tokenizer(value, truncation=False)['input_ids']) <= self.encoder.max_seq_length:
                return value
            if len(history) == 1:
                raise ValueError('Latest user turn and metadata exceed encoder context')
            history.pop(0)

    def predict(self, case):
        vector = self.encoder.encode([self.input_text(case)], normalize_embeddings=True)
        probs = dict(zip(self.classifier.classes_, self.classifier.predict_proba(vector)[0].tolist()))
        return {'intent': max(probs, key=probs.get), 'logits': [math.log(max(probs[k], 1e-300)) for k in LABELS]}


def local_one(case, group, scorer, order, temperature=1.):
    start = time.perf_counter()
    row = {'id': case['id'], 'response': None, 'error': None, 'attempts': 0, 'usage': [], 'route_seconds': None}
    try:
        if group == 'C0':
            result = {'intent': scorer.generate(*model_inputs(case))}
        elif group == 'B':
            result = scorer.predict(case)
        else:
            result = scorer.score(*model_inputs(case), order=order, temperature=temperature)
        row['response'] = {'intent': result.pop('intent')}
        row.update(result)
    except Exception as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)[:500]
    row['seconds'] = row['intent_seconds'] = time.perf_counter()-start
    return row


def evaluation_identity(args, config):
    """Validate effective inputs before any formal slot or model inference."""
    group = group_method(args.group)
    if args.adapter and group not in {'D', 'E'}:
        raise ValueError('Adapters are only allowed for D/E')
    if args.config and group not in {'C1', 'D', 'E'}:
        raise ValueError('Calibration config is only allowed for C1/D/E')
    if group == 'C0' and args.order != 'forward':
        raise ValueError('C0 generation uses forward order')
    if config:
        validate_config(config)
        if not config.get('calibrated'):
            raise ValueError('Evaluation requires calibrated config')
        for key in ('order', 'max_chars'):
            if config[key] != getattr(args, key):
                raise ValueError('Evaluation calibration identity mismatch: ' + key)
    local = group != 'A'
    identity = {'order': args.order, 'max_chars': args.max_chars,
                'adapter_sha256': weight_identity(args.adapter) if args.adapter else None,
                'config_sha256': sha(args.config) if args.config else None,
                'weights_sha256': weight_identity(args.encoder if group == 'B' else args.model) if local else None,
                'prompt_sha256': prompt_hash() if group in {'C0', 'C1', 'D', 'E'} else None,
                'model_id': args.remote_model, 'remote_model': None, 'endpoint_host': None,
                'embedding_context': 'native_limit_drop_oldest_preserve_latest' if group == 'B' else None}
    if config:
        for key in ('weights_sha256', 'adapter_sha256', 'prompt_sha256'):
            if config.get(key) != identity[key]:
                raise ValueError('Evaluation calibration identity mismatch: ' + key)
    if group in {'A', 'E'} and not (group == 'A' and args.split == 'regression_v1'):
        load_dotenv(ROOT/'.env')
        remote = model_from_env(args.remote_model)
        identity.update(remote_model=remote.name, endpoint_host=urlsplit(remote.base_url).hostname)
    return identity


def reserve_holdout(panel_sha, group, out, plan, plan_sha):
    directory = ROOT/'data/decision-router-holdout'/panel_sha
    directory.mkdir(parents=True, exist_ok=True)
    frozen = directory/'plan.json'
    record = {'plan_sha256': plan_sha, 'plan': plan}
    with tempfile.NamedTemporaryFile('w', dir=directory, prefix='plan-', suffix='.tmp', encoding='utf-8', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(record, stream, ensure_ascii=False)
    try:
        # Publish a complete file atomically without replacing an existing plan.
        try:
            os.link(temporary, frozen)
        except FileExistsError:
            if read(frozen) != record:
                raise ValueError('Formal evaluation plan changed after the first group started')
    finally:
        temporary.unlink()
    path = directory/(group + '.json')
    # An interrupted/failed attempt still consumes the first-run slot.
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'group': group, 'out': str(out.resolve()), 'started_at': time.time(),
                   'panel_sha256': panel_sha, 'plan_sha256': plan_sha}, stream)


def run(args):
    group = group_method(args.group)
    if args.split == 'test_v3':
        from evals.build_intent_routing_v3 import verify_frozen as verify_v3
        if args.group not in V3_GROUPS:
            raise ValueError('Group not permitted on test_v3')
        panel, path = verify_v3(), ROOT/'datasets/intent_routing/v3.json'
    else:
        if args.group in V3_GROUPS - {'A', 'D'}:
            raise ValueError('Follow-up group names require test_v3')
        panel, path = verify_frozen(), ROOT/'datasets/intent_routing/v2.json'
    if args.split == 'regression_v1':
        path = ROOT/'datasets/intent_routing/v1.json'
        panel_cases = read(path)['cases']
        cases = [c for c in panel_cases if c['split'] == 'test']
    else:
        cases = [c for c in panel['cases'] if c['split'] == args.split]
    if group == 'D' and not args.adapter:
        raise ValueError('D requires a LoRA adapter')
    if group == 'E' and not args.config:
        raise ValueError('E requires frozen calibration config')
    if args.out.exists():
        raise FileExistsError(args.out)
    config = read(args.config) if args.config else None
    identity = evaluation_identity(args, config)
    temperature = config['temperature'] if config and group in {'C1', 'D'} else 1.
    limits = V1_BUDGETS['test'] if args.split == 'regression_v1' else LIMITS
    if args.split in {'test_v2', 'test_v3'}:
        if not args.plan:
            raise ValueError('Formal test requires a dev-selected frozen evaluation plan')
        plan = read(args.plan)
        if plan['panel_sha256'] != sha(path):
            raise ValueError('Evaluation plan dataset mismatch')
        if args.split == 'test_v3':
            required = {'A', 'D', 'E2', 'C1-4B'}
            if plan['four_b_selection']['include_cascade']:
                required.add('E2-4B')
            if set(plan['groups']) != required or plan['source_files'] != source_manifest():
                raise ValueError('Frozen v3 groups or source files changed')
        expected = plan['groups'][args.group]
        if expected != identity:
            raise ValueError('Test parameters differ from frozen dev selection')
    args.out.mkdir(parents=True, exist_ok=False)
    protocol = {'schema_version': 1, 'group': args.group, 'split': args.split, 'panel_sha256': sha(path),
        'source_files': source_manifest(), 'hardware': hardware(), 'order': args.order, 'max_chars': args.max_chars,
        'model': args.model.name if group in {'C0', 'C1', 'D', 'E'} else args.encoder.name+'+LogisticRegression' if group == 'B' else args.remote_model,
        'model_path': str(args.model), 'adapter_path': str(args.adapter) if args.adapter else None, 'temperature': temperature,
        'remote_model_id': args.remote_model, 'config_sha256': sha(args.config) if args.config else None,
        'plan_sha256': sha(args.plan) if args.plan else None, 'budget': limits if group in {'A', 'E'} else None,
        'warmup_requests': 0 if group == 'A' and args.split == 'regression_v1' else 5,
        'seed': 17, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'comparison': 'Local GPU classifier versus remote API; different hardware/deployment. Only A/E measure complete routes.',
        'interpretation': 'regression set, not held out' if args.split == 'regression_v1' else 'Project taxonomy; not official MASSIVE or end-to-end task success.'}
    protocol['evaluation_identity'] = identity
    write(args.out/'protocol.json', protocol)
    if args.split in {'test_v2', 'test_v3'}:
        reserve_holdout(sha(path), args.group, args.out, plan, sha(args.plan))
    rows, scorer, budget = [], None, None
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        start = time.perf_counter()
        if group in {'C0', 'C1', 'D', 'E'}:
            scorer = OptionScorer(args.model, adapter=args.adapter, device=args.device, max_chars=args.max_chars)
            protocol.update(weights_sha256=scorer.weights_sha256, adapter_sha256=scorer.adapter_sha256,
                            prompt_sha256=scorer.prompt_sha256, tokenization=scorer.tokenization,
                            cold_start_seconds=scorer.cold_start_seconds)
            if config:
                for key in ('weights_sha256', 'adapter_sha256', 'prompt_sha256'):
                    if config.get(key) != getattr(scorer, key):
                        raise ValueError('Evaluation calibration identity mismatch: ' + key)
        elif group == 'B':
            scorer = EmbeddingClassifier(panel, args.encoder, args.device, args.max_chars)
            protocol.update(weights_sha256=identity['weights_sha256'], cold_start_seconds=scorer.cold_start_seconds,
                            classifier_fit_seconds=scorer.classifier_fit_seconds,
                            encoder_max_tokens=scorer.encoder.max_seq_length,
                            logistic_regression={'C': 1., 'max_iter': 1000, 'seed': 17, 'training_split': 'train_v2'})
            write(args.out/'classifier.json', {'classes': scorer.classifier.classes_.tolist(),
                'coefficients': scorer.classifier.coef_.tolist(), 'intercepts': scorer.classifier.intercept_.tolist()})
        if scorer:
            synthetic = {'history': [{'role': 'user', 'content': '请解释矩阵乘法的基本含义。'}],
                         'space': {'name': '预热', 'description': '通用问答', 'local_material_count': 0}, 'search_scope': 'auto'}
            protocol['local_warmup_seconds'] = []
            for _ in range(5):
                warmup_start = time.perf_counter()
                if group == 'B':
                    scorer.predict(synthetic)
                elif group == 'C0':
                    try:
                        scorer.generate(*model_inputs(synthetic))
                    except ValueError:
                        pass  # Invalid generation is still a completed warmup forward.
                else:
                    scorer.score(*model_inputs(synthetic), order=args.order)
                protocol['local_warmup_seconds'].append(time.perf_counter()-warmup_start)
        write(args.out/'protocol.json', protocol)
        if group in {'A', 'E'}:
            if group == 'A' and args.split == 'regression_v1':
                rows = read(ROOT/'docs/research/intent-routing-results-20261012.json')['rows']
                rows = [{**r, 'intent_seconds': r['seconds'], 'route_seconds': r['seconds']} for r in rows]
                protocol['reuse'] = '10-12 first responses, audited labels; zero new requests'
            else:
                load_dotenv(ROOT/'.env')
                remote = model_from_env(args.remote_model)
                protocol.update(remote_model=remote.name, endpoint_host=urlsplit(remote.base_url).hostname)
                budget = Budget(limits)
                router = DecisionRouter(scorer, config) if group == 'E' else None
                # API warmups share the run budget and are retained as cost receipts.
                warmups = [{'id': f'warmup-{i}', 'history': [{'role': 'user', 'content': '请用一句话解释矩阵乘法。'}],
                            'space': {'name': '预热', 'description': '通用知识', 'local_material_count': 0}, 'search_scope': 'auto'} for i in range(5)]
                protocol['remote_warmup_results'] = []
                for case in warmups:
                    try:
                        row = guarded_remote(case, args.out/'warmup', args.remote_model, budget,
                                             router if args.split == 'test_v3' else None)
                    except Exception as exc:
                        row = failed_remote_receipts(case, args.out/'warmup', exc)
                    protocol['remote_warmup_results'].append(row)
                write(args.out/'protocol.json', protocol)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [(c, pool.submit(guarded_remote, c, args.out, args.remote_model, budget, router)) for c in cases]
                    for case, future in futures:
                        try:
                            row = future.result()
                        except Exception as exc:
                            row = failed_remote_receipts(case, args.out, exc)
                        rows.append(row)
                        try:
                            with (args.out/'results.jsonl').open('a', encoding='utf-8') as stream:
                                stream.write(json.dumps(row, ensure_ascii=False)+'\n')
                        except OSError as exc:
                            protocol.setdefault('journal_errors', []).append(type(exc).__name__+': '+str(exc))
                        print(f'{args.group} {args.split} {len(rows)}/{len(cases)}', flush=True)
        else:
            for case in cases:
                row = local_one(case, args.group, scorer, args.order, temperature)
                rows.append(row)
                with (args.out/'results.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False)+'\n')
                if len(rows) % 10 == 0:
                    print(f'{args.group} {args.split} {len(rows)}/{len(cases)}', flush=True)
    except Exception as exc:
        protocol['run_error'] = type(exc).__name__ + ': ' + str(exc)[:1000]
    finally:
        indexed = {r['id'] for r in rows}
        for case in cases:
            if case['id'] not in indexed:
                rows.append({'id': case['id'], 'response': None, 'error': protocol.get('run_error', 'missing result'),
                             'attempts': 0, 'seconds': 0, 'usage': []})
        if budget:
            protocol['budget_final'] = budget.snapshot()
        if 'torch' in locals() and torch.cuda.is_available():
            protocol['peak_gpu_memory_bytes'] = torch.cuda.max_memory_allocated()
        protocol['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        write(args.out/'protocol.json', protocol)
        write(args.out/'results.json', rows)
        protocol['results_sha256'] = sha(args.out/'results.json')
        write(args.out/'protocol.json', protocol)
        metrics = summarize(cases, rows, temperature, cascade=group == 'E')
        write(args.out/'metrics.json', metrics)
        if metrics['calibration']['probability_cases']:
            diagrams(metrics['calibration'], args.out)
        lines = [f'# Decision router {args.group} / {args.split}', '',
                 f'Accuracy {metrics["correct"]}/{metrics["n"]}; macro-F1 {metrics["macro_f1"]:.6f}.',
                 f'Failures/missing: {metrics["errors_or_missing"]}; AUTO false positives: {metrics["auto_research_false_positives"]}.', '',
                 protocol['comparison'], protocol['interpretation'],
                 'All failures remain in the denominator. Probability metrics cover only successful probability outputs; missing counts are explicit.']
        (args.out/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
        print(json.dumps({'group': args.group, 'accuracy': metrics['accuracy'], 'macro_f1': metrics['macro_f1'],
                          'errors': metrics['errors_or_missing'], 'run_error': protocol.get('run_error')}, ensure_ascii=False), flush=True)
    if protocol.get('run_error'):
        raise RuntimeError(protocol['run_error'])


def report_study(study, destination):
    """Recompute all claims from saved first responses, never from hand-entered scores."""
    panel = verify_frozen()
    cases = [c for c in panel['cases'] if c['split'] == 'test_v2']
    regression_cases = [c for c in read(ROOT/'datasets/intent_routing/v1.json')['cases'] if c['split'] == 'test']
    selection = read(study/'model-selection.json')
    bundle = {'schema_version': 1, 'panel_sha256': sha(ROOT/'datasets/intent_routing/v2.json'),
              'study_protocol': read(study/'study-protocol.json'), 'test_plan': read(study/'test-plan.json'),
              'order_selection': read(study/'dev-order-selection.json'), 'model_selection': selection,
              'training_curve': read(Path(selection['training_run'])/'training-curve.json'),
              'training_protocol': read(Path(selection['training_run'])/'protocol.json'),
              'api_preflight': read(study/'api-preflight'/'result.json'),
              'data_review': panel['label_audit'], 'dedup': panel['dedup'], 'groups': {}, 'regression_v1': {},
              'limitations': panel['limits'] + [
                  'Local GPU versus remote API is a deployment comparison, not identical-hardware model speed.',
                  'Only one training seed was run; paired bootstrap does not estimate seed variability.',
                  '70 dev cases are too few to certify 2% production risk; thresholds are empirical selection rules.',
                  'ECE/Brier/NLL/AURC cover successful probability outputs only, with missing counts explicit.',
                  'A/C0/E do not expose a calibrated distribution for their final decision; no invented confidence metrics.']}
    for group in ('A', 'B', 'C0', 'C1', 'D', 'E'):
        folder = study/f'test-{group}'
        protocol, rows = read(folder/'protocol.json'), read(folder/'results.json')
        if (protocol['panel_sha256'] != bundle['panel_sha256'] or protocol['split'] != 'test_v2' or protocol['group'] != group
                or protocol['plan_sha256'] != sha(study/'test-plan.json')
                or protocol['evaluation_identity'] != bundle['test_plan']['groups'][group]):
            raise ValueError('Mismatched formal evaluation artifact')
        metrics = summarize(cases, rows, protocol.get('temperature', 1.), cascade=group == 'E')
        bundle['groups'][group] = {'protocol': protocol, 'rows': rows, 'metrics': metrics,
                                   'results_sha256': sha(folder/'results.json')}
        folder = study/f'regression-{group}'
        protocol, rows = read(folder/'protocol.json'), read(folder/'results.json')
        bundle['regression_v1'][group] = {'protocol': protocol, 'rows': rows,
            'metrics': summarize(regression_cases, rows, protocol.get('temperature', 1.), cascade=group == 'E'), 'results_sha256': sha(folder/'results.json')}
    bundle['paired_comparisons'] = {f'{a}_minus_{b}': paired_comparison(cases, bundle['groups'][a]['rows'], bundle['groups'][b]['rows'])
                                    for a, b in (('E', 'A'), ('D', 'C1'))}
    bundle['calibration'] = {name: read(study/f'calibration-{name}'/'selection.json') for name in ('C1', 'D')}
    bundle['configs'] = {name: read(study/f'calibration-{name}'/'config.json') for name in ('C1', 'D')}
    bundle['dev_runs'] = {name: {'metrics': read(study/name/'metrics.json'), 'protocol': read(study/name/'protocol.json')}
                          for name in ('dev-C1-forward', 'dev-C1-reverse', 'dev-C1-average', 'dev-C0-forward', 'dev-B-forward', 'dev-B-native-context', 'dev-D-average')}
    destination.parent.mkdir(parents=True, exist_ok=True)
    assets = destination.parent/(destination.name+'-assets')
    if destination.with_suffix('.json').exists() or destination.with_suffix('.md').exists() or assets.exists():
        raise FileExistsError(destination)
    assets.mkdir()
    for group in ('B', 'C1', 'D', 'dev-C1', 'dev-D'):
        folder = assets/group
        folder.mkdir()
        metrics = (bundle['calibration'][group[4:]]['after'] if group.startswith('dev-')
                   else bundle['groups'][group]['metrics']['calibration'])
        diagrams(metrics, folder)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    losses = [json.loads(line) for path in sorted(Path(selection['training_run']).glob('lr-*/loss.jsonl'))
              for line in path.read_text(encoding='utf-8').splitlines()]
    write(assets/'training-plot-data.json', {'steps': losses, 'checkpoints': bundle['training_curve']})
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), layout='constrained')
    for lr in bundle['training_protocol']['learning_rates']:
        points = [p for p in bundle['training_curve'] if p['learning_rate'] == lr]
        for ax, key, label in zip(axes, ('loss', 'dev_macro_f1', 'dev_ece'), ('Cumulative mean training loss', 'Dev macro-F1', 'Dev ECE (raw)')):
            values = [p['train_means']['loss'] if key == 'loss' else p[key] for p in points]
            ax.plot([p['epoch'] for p in points], values, '-o', label=f'lr={lr:g}')
            ax.set(xlabel='Epoch', ylabel=label)
    axes[2].axhline(bundle['training_protocol']['baseline_ece']+.03, linestyle='--', color='gray', label='ECE eligibility ceiling')
    for ax in axes:
        ax.legend(fontsize=8)
    fig.savefig(assets/'training.png', dpi=170)
    plt.close(fig)
    bundle['figure_files'] = {p.relative_to(destination.parent).as_posix(): sha(p) for p in assets.rglob('*') if p.is_file()}
    write(destination.with_suffix('.json'), bundle)
    def pct(value):
        return '—' if value is None else f'{100*value:.2f}%'
    def number(value):
        return '—' if value is None else f'{value:.3f}'
    lines = ['# 决策模型式意图路由：首次冻结评测', '',
        f'数据 SHA-256：`{bundle["panel_sha256"]}`。主结论来自140题 test_v2，七类各20题；每组只运行一次，失败不剔除。',
        '不调用 Jev，也不是 Jev 产品对比。标签由独立代理编写和盲审，不是人工标注；成绩不是端到端研究任务成功率。', '',
        '|组|准确率|macro-F1|EXPERIMENT recall|RESEARCH recall|AUTO recall|AUTO误触发|非CHAT→CHAT|对抗准确率|失败|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for group, data in bundle['groups'].items():
        m = data['metrics']
        lines.append(f'|{group}|{m["correct"]}/{m["n"]} ({pct(m["accuracy"])})|{pct(m["macro_f1"])}|{pct(m["per_class"]["EXPERIMENT"]["recall"])}|{pct(m["per_class"]["RESEARCH"]["recall"])}|{pct(m["per_class"]["AUTO_RESEARCH"]["recall"])}|{m["auto_research_false_positives"]}/{m["auto_research_negative_cases"]}|{m["non_chat_predicted_chat"]}|{pct(m["tags"].get("adversarial",{}).get("accuracy"))}|{m["errors_or_missing"]}|')
    lines += ['', 'A=远程LLM；B=BGE-M3+逻辑回归；C0=0.6B贪心生成；C1=免训练选项打分；D=LoRA打分；E=开发集选择的本地模型加LLM级联。', '',
              '## 时延与成本', '',
              '本地 GPU 小模型 vs 远程 API；不是同等硬件条件的模型速度比较。冷启动单列，热态预热5次。只有A/E包含字段补全或回退，能用于完整路由时间比较。', '',
              '|组|冷启动秒|意图p50/p95秒|完整路由p50/p95秒|远程物理请求|已报告token|峰值显存GiB|',
              '|---|---:|---:|---:|---:|---:|---:|']
    for group, data in bundle['groups'].items():
        m, p = data['metrics'], data['protocol']
        charged_rows = data['rows'] + p.get('remote_warmup_results', [])
        attempts = sum(r.get('attempts', 0) for r in charged_rows)
        tokens = sum(u.get('total_tokens') or 0 for r in charged_rows for u in r.get('usage', []))
        lines.append(f'|{group}|{number(p.get("cold_start_seconds"))}|{number(m["intent_seconds"]["p50"])}/{number(m["intent_seconds"]["p95"])}|{number(m["route_seconds"]["p50"])}/{number(m["route_seconds"]["p95"])}|{attempts}|{tokens}|{number(p.get("peak_gpu_memory_bytes",0)/2**30)}|')
    lines += ['', f'B 的编码器加载与拟合分开计时，训练向量编码和逻辑回归拟合用时 {number(bundle["groups"]["B"]["protocol"].get("classifier_fit_seconds"))} 秒。A/E请求和token包含5次远程预热；逐题热态时延不包含预热。']
    cascade = bundle['groups']['E']['metrics']['cascade']
    lines += ['', f'级联回退率 {pct(cascade["fallback_rate"])}（{cascade["decision_observations"]}条可观测决策，{cascade["unobserved_cases"]}条未观测）；回退部分准确率 {pct((cascade["fallback"] or {}).get("accuracy"))}，接受部分准确率 {pct((cascade["accepted"] or {}).get("accuracy"))}。字段补全 {cascade["field_completion_count"]} 次。',
              '未知token用量不当作零成本；完整用量覆盖率、保守预留记账、请求失败和逐题时延见JSON。', '',
              '## 校准与选择', '', '|组|ECE（15箱）|Brier|NLL|AURC|有效概率样本|', '|---|---:|---:|---:|---:|---:|']
    for group in ('B', 'C1', 'D'):
        c = bundle['groups'][group]['metrics']['calibration']
        lines.append(f'|{group}|{number(c["ece"])}|{number(c["brier"])}|{number(c["nll"])}|{number(c["aurc"])}|{c["probability_cases"]}/140|')
    lines += ['', '|开发集方案|macro-F1|原始ECE|意图p95秒|', '|---|---:|---:|---:|']
    for name, data in bundle['dev_runs'].items():
        m = data['metrics']
        lines.append(f'|{name}|{pct(m["macro_f1"])}|{number(m["calibration"]["ece"])}|{number(m["intent_seconds"]["p95"])}|')
    lines += ['', f'顺序方案：{bundle["order_selection"]["selected"]["order"]}；LoRA和级联选择记录、三个学习率的全部检查点、温度/阈值与完整risk–coverage网格见JSON。',
              '早期 dev-B-forward 曾强制512 token右截断，可能丢失最新用户消息；审查在正式测试前发现并修复。该运行原样保留，修正后的开发运行是 dev-B-native-context。B正式组使用编码器原生上限，必要时移除旧历史，最新消息放不下则失败，禁止静默截断。',
              'C1/D的表中校准指标使用dev冻结温度；B为原始逻辑回归概率。A/C0/E不具备最终决定的概率分布，不伪造校准分数。', '',
              f'![训练与开发集曲线]({assets.name}/training.png)', '',
              f'![C1 测试集校准]({assets.name}/C1/calibration.png)', '',
              f'![D 测试集校准]({assets.name}/D/calibration.png)', '',
              '## 配对统计', '', '|比较|准确率差|10,000次配对bootstrap 95%区间|McNemar精确双侧p|', '|---|---:|---:|---:|']
    for name, p in bundle['paired_comparisons'].items():
        lines.append(f'|{name}|{pct(p["accuracy_difference_first_minus_second"])}|[{pct(p["paired_bootstrap_95"][0])}, {pct(p["paired_bootstrap_95"][1])}]|{p["mcnemar_exact_two_sided_p"]:.6g}|')
    lines += ['', '样本含模板/主题相关性，区间仅作描述，不能视为总体泛化保证。', '', '## 捷径监测', '',
              '|组|含实验/代码但不是执行类|执行类且不含实验/代码|CHAT预测数|CHAT平均概率|', '|---|---:|---:|---:|---:|']
    for group in ('C1', 'D'):
        m = bundle['groups'][group]['metrics']
        s, c = m['keyword_shortcuts'], m['calibration']
        lines.append(f'|{group}|{pct((s["keyword_non_execution"] or {}).get("accuracy"))}|{pct((s["execution_without_keywords"] or {}).get("accuracy"))}|{c.get("predicted_counts",{}).get("CHAT",0)}|{number(c.get("class_mean_probability",{}).get("CHAT"))}|')
    lines += ['', '七类预测数相对gold偏移、训练前后每类平均概率、ECE和关键词分层完整保存在JSON。', '',
              '## v1 回归（非保留测试）', '', '|组|准确率|macro-F1|失败|', '|---|---:|---:|---:|']
    for group, data in bundle['regression_v1'].items():
        m = data['metrics']
        lines.append(f'|{group}|{m["correct"]}/{m["n"]} ({pct(m["accuracy"])})|{pct(m["macro_f1"])}|{m["errors_or_missing"]}|')
    lines += ['', 'A组直接复用2026-10-12首次响应和已审计标签，无新增调用。其他组是已见回归集上的首次本轮运行，不能作为新留出证据。', '', '## 失败与局限', '']
    for group, data in bundle['groups'].items():
        lines.append(f'- {group}：错误或失败ID：' + (', '.join(r['id'] for r in data['metrics']['failures']) or '无') + '。')
    lines += [''] + ['- ' + note for note in bundle['limitations']]
    destination.with_suffix('.md').write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')
    print(str(destination.with_suffix('.md')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hardware', action='store_true')
    parser.add_argument('--summarize', type=Path, help='Study directory with all six test and regression artifacts')
    parser.add_argument('--report-prefix', type=Path, help='New .md/.json report path without suffix')
    parser.add_argument('--group', choices=['A', 'B', 'C0', 'C1', 'D', 'E', 'E2', 'C1-4B', 'E2-4B'])
    parser.add_argument('--split', choices=['dev_v2', 'test_v2', 'test_v3', 'regression_v1'], default='dev_v2')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--model', type=Path, default=ROOT/'data/models/Qwen3-0.6B')
    parser.add_argument('--encoder', type=Path, default=ROOT/'data/models/bge-m3')
    parser.add_argument('--adapter', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--remote-model', default='sudocode-luna')
    parser.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='auto')
    parser.add_argument('--order', choices=['forward', 'reverse', 'average'], default='forward')
    parser.add_argument('--max-chars', type=int, default=6000)
    args = parser.parse_args()
    if args.summarize:
        if not args.report_prefix:
            parser.error('--report-prefix required with --summarize')
        report_study(args.summarize, args.report_prefix)
    elif args.hardware:
        print(json.dumps(hardware(), ensure_ascii=False, indent=2))
    else:
        if not args.group or not args.out:
            parser.error('--group and --out required')
        run(args)


if __name__ == '__main__':
    main()
