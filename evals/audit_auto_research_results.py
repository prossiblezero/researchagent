"""Recompute saved experiment results without importing generated experiment code."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.audit_scifact_predictions import read_jsonl, score_predictions
from evals.run_mixed_benchmark import retrieval_metrics
from research_agent.coding_tool import protected_hash, qasper_ranking_rows, regular_file, workspace_artifact_path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def score_rankings(tasks, corpus, rows, seeds):
    """Require complete seed/task coverage and use fraction-of-gold recall, not hit rate."""
    by_id = {t['id']: t for t in tasks}
    contexts = {p['id']: p for p in corpus}
    if len(by_id) != len(tasks) or not tasks:
        raise ValueError('Expected unique nonempty task IDs')
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= 64 or any(type(s) is not int for s in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError('Expected unique integer seeds')
    expected = {(seed, ident) for seed in seeds for ident in by_id}
    scored = {}
    for row in rows:
        key = (row['seed'], row['task_id'])
        if type(row['seed']) is not int or key not in expected or key in scored:
            raise ValueError('Ranking must cover every seed/task exactly once')
        task = by_id[row['task_id']]
        hits = [r['paragraph_id'] for r in row['rankings']]
        if len(hits) != len(set(hits)) or any(
                h not in contexts or contexts[h]['source_id'] != task['source_id'] for h in hits):
            raise ValueError('Invalid, duplicate or out-of-paper paragraph IDs')
        value = retrieval_metrics(task, hits)
        if not value['eligible']:
            raise ValueError('Task lacks mapped gold evidence')
        scored[key] = {'seed': key[0], 'task_id': key[1], 'recall_at_5': value['recall_at5'],
                       'mrr_at_5': value['mrr_at5'], 'hit_at_5': float(value['recall_at5'] > 0),
                       'hits': value['hits']}
    if set(scored) != expected:
        raise ValueError('Missing seed/task predictions')
    per_seed = [{'seed': seed, **{name: sum(scored[seed, ident][name] for ident in by_id) / len(by_id)
                                for name in ('recall_at_5', 'mrr_at_5', 'hit_at_5')}} for seed in seeds]
    return {'metrics': {name: sum(s[name] for s in per_seed) / len(seeds)
                        for name in ('recall_at_5', 'mrr_at_5', 'hit_at_5')},
            'per_seed': per_seed, 'rows': list(scored.values()),
            'identical_top5_across_seeds': all(scored[seeds[0], ident]['hits'] == scored[seed, ident]['hits']
                                             for seed in seeds for ident in by_id)}


def audit(run, output, dataset):
    if dataset not in {'scifact', 'qasper', 'locomo'}:
        raise ValueError('Unsupported audit dataset')
    output.mkdir(parents=True, exist_ok=False)
    inputs = read(run / 'inputs.json')
    workspace = Path(inputs['workspace'])
    tasks = read(run / 'coding-tasks.json')
    if dataset == 'scifact':
        data = read_jsonl(workspace / 'data/scifact/claims_dev.jsonl')
        expected = read(ROOT / 'datasets/open/catalog.json')['scifact']['raw_sha256']['claims_dev.jsonl']
        if hashlib.sha256((workspace / 'data/scifact/claims_dev.jsonl').read_bytes()).hexdigest() != expected:
            raise ValueError('Official SciFact dev hash changed')
    else:
        frozen_inputs = inputs['inputs']
        if dataset == 'locomo':
            case = read(run / 'cases.json')['campaign']
            scoring_paths = {'data/locomo/tasks.json', 'data/locomo/corpus.json'}
            frozen_inputs = [item for item in frozen_inputs if item['destination'] in scoring_paths]
            if len(frozen_inputs) != 2 or {item['destination'] for item in frozen_inputs} != scoring_paths:
                raise ValueError('Missing or duplicate frozen LoCoMo scoring inputs')
            # Initial resumable caches can legitimately grow. Check scoring data
            # and explicitly frozen sources, not every copied runtime input.
            for name, expected in case.get('frozen_sources', {}).items():
                if protected_hash(workspace, name) != expected:
                    raise ValueError('Frozen LoCoMo source changed: ' + name)
        for item in frozen_inputs:
            if hashlib.sha256(regular_file(workspace, item['destination'], limit=16*1024*1024)).hexdigest() != item['sha256']:
                raise ValueError('Frozen audit inputs changed')
        if dataset == 'locomo':
            from research_agent.locomo_metrics import score_predictions as score_locomo
            data = read(workspace / 'data/locomo/tasks.json')
            corpus = read(workspace / 'data/locomo/corpus.json')
            job = read(run / 'job.json')
            label_raw = regular_file(run, f"auto-research/{job['id']}/scoring-labels.json", limit=16*1024*1024)
            if hashlib.sha256(label_raw).hexdigest() != case['labels_sha256']:
                raise ValueError('Frozen LoCoMo labels changed')
            labels = json.loads(label_raw)
        else:
            data = read(workspace / 'data/qasper/tasks.json')['tasks']
            corpus = read(workspace / 'data/qasper/corpus.json')
    rows = []
    for task in tasks:
        for index, m in enumerate(task['state'].get('measurements', [])):
            if not m['valid']:
                continue
            result = m['result']
            if dataset == 'qasper' and not isinstance(result.get('config'), dict):
                raise ValueError('QASPER measurement is missing config')
            if dataset == 'qasper' and not str(result['config'].get('dataset', '')).lower().startswith('qasper'):
                raise ValueError('QASPER measurement dataset does not match the host audit contract')
            diagnostics = result.get('diagnostics') or {}
            field = ('predictions_path' if dataset in {'scifact', 'locomo'} else
                     'ranking_file' if diagnostics.get('ranking_file') else 'ranking_path')
            if 'prediction_artifacts' in m:
                artifact = m['prediction_artifacts'].get(field)
                if not isinstance(artifact, dict):
                    raise ValueError(f'Missing saved {field} artifact')
                raw = regular_file(run, f"coding-tools/{task['id']}/{artifact['path']}", limit=16*1024*1024)
                if hashlib.sha256(raw).hexdigest() != artifact.get('sha256'):
                    raise ValueError('Saved prediction artifact hash changed')
                origin = 'measurement_snapshot'
            else:
                # Legacy receipts predate immutable prediction artifacts. Keep the
                # fallback explicit; a missing declared snapshot never falls back.
                path_value = diagnostics.get(field)
                if not isinstance(path_value, str) or not path_value.strip():
                    raise ValueError(f'Missing {field} in measurement diagnostics')
                path = workspace_artifact_path(workspace, path_value)
                raw = regular_file(workspace, path, limit=16*1024*1024)
                origin = 'legacy_workspace'
            text = raw.decode('utf-8')
            if dataset == 'qasper':
                predictions = qasper_ranking_rows(text, data, result['config']['seeds'])
            else:
                predictions = [json.loads(line) for line in text.splitlines() if line.strip()]
            if dataset == 'scifact':
                predicted = [{'id': p['id'], 'gold': p['gold'], 'prediction': p['pred']} for p in predictions]
                recomputed = score_predictions(data, predicted)
            elif dataset == 'locomo':
                config = result.get('config')
                if (not isinstance(config, dict) or config.get('dataset') != 'locomo'
                        or config.get('seeds') != case['seeds']
                        or config.get('dataset_version') != hashlib.sha256(regular_file(workspace, 'data/locomo/tasks.json')).hexdigest()
                        or any(t.get('split') != config.get('split') for t in data)):
                    raise ValueError('LoCoMo result config does not match its frozen dataset/split/seed contract')
                recomputed = score_locomo(data, corpus, labels, predictions, result['config'].get('seeds'))
            else:
                recomputed = score_rankings(data, corpus, predictions, result['config']['seeds'])
            reported = m.get('metrics') if isinstance(m.get('metrics'), dict) else {}
            differences = {}
            for key, independent in recomputed['metrics'].items():
                value = reported.get(key)
                if type(value) not in (int, float) or not math.isfinite(value):
                    differences[key] = {'reported': value, 'independent': independent, 'error': 'missing_or_nonfinite'}
                elif not math.isclose(value, independent, abs_tol=1e-9):
                    differences[key] = {'reported': value, 'independent': independent}
            artifact = f'{task["id"]}-{index}.jsonl'
            (output / artifact).write_bytes(raw)
            rows.append({'task_id': task['id'], 'name': m['name'], 'role': m['role'],
                         'plan_version': json.loads(task['request']['plan'])['version'],
                         'predictions': artifact, 'prediction_sha256': hashlib.sha256(raw).hexdigest(),
                         'prediction_origin': origin,
                         'script_sha256': m['script_sha256'], 'config': result['config'],
                         'reported_metrics': m['metrics'], 'differences': differences, **recomputed})
    result = {'dataset': dataset, 'measurements': rows, 'scores_reproduced': bool(rows) and not any(r['differences'] for r in rows),
              'scope': 'Independent saved-prediction scoring and sample/seed coverage; no generated code imported; not scientific novelty or held-out validation.'}
    (output / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dataset', choices=['scifact', 'qasper', 'locomo'], required=True)
    args = parser.parse_args()
    result = audit(args.run, args.output, args.dataset)
    print(json.dumps({'scores_reproduced': result['scores_reproduced'],
                      'measurements': len(result['measurements']), 'output': str(args.output)}))
