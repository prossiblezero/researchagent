"""Fit temperature and selective-routing thresholds exclusively on frozen dev_v2."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent.decision_router import LABELS, probabilities
from evals.build_intent_routing_v2 import read, write, sha, verify_frozen
from evals.run_intent_routing_eval import score


def distribution_metrics(cases, rows, temperature=1.0):
    by_id = {r['id']: r for r in rows}
    valid = [(c, probabilities(by_id[c['id']]['logits'], temperature)) for c in cases
             if c['id'] in by_id and by_id[c['id']].get('logits') is not None and not by_id[c['id']].get('error')]
    if not valid:
        return {'probability_cases': 0, 'missing_probability_cases': len(cases), 'ece': None, 'brier': None, 'nll': None, 'aurc': None,
                'reliability': [], 'risk_coverage': []}
    bins = [{'lower': i/15, 'upper': (i+1)/15, 'n': 0, 'correct': 0, 'confidence_sum': 0.0} for i in range(15)]
    nll, brier, ranked = 0., 0., []
    means = [0.] * 7
    predicted, gold = Counter(), Counter()
    for c, p in valid:
        target = LABELS.index(c['expected_intent'])
        winner = max(range(7), key=p.__getitem__)
        confidence, correct = p[winner], int(winner == target)
        bucket = bins[min(14, int(confidence * 15))]
        bucket['n'] += 1
        bucket['correct'] += correct
        bucket['confidence_sum'] += confidence
        nll -= math.log(max(p[target], 1e-300))
        brier += sum((v - int(i == target))**2 for i, v in enumerate(p))
        ranked.append((confidence, 1 - correct))
        predicted[LABELS[winner]] += 1
        gold[LABELS[target]] += 1
        means = [a+b for a, b in zip(means, p)]
    n = len(valid)
    for b in bins:
        b['accuracy'] = b['correct'] / b['n'] if b['n'] else None
        b['confidence'] = b['confidence_sum'] / b['n'] if b['n'] else None
    ece = sum(b['n'] * abs(b['accuracy'] - b['confidence']) for b in bins if b['n']) / n
    ranked.sort(reverse=True)
    curve, total, errors, area = [], 0, 0, 0.
    for threshold in sorted({p for p, _ in ranked}, reverse=True):
        group = [error for p, error in ranked if p == threshold]
        # Ties are accepted together; their ordering cannot improve AURC.
        before = total
        total += len(group)
        errors += sum(group)
        risk = errors / total
        area += risk * (total - before) / n
        curve.append({'threshold': threshold, 'coverage': total/n, 'risk': risk, 'accepted': total, 'errors': errors})
    return {'probability_cases': n, 'missing_probability_cases': len(cases)-n, 'ece': ece, 'brier': brier/n, 'nll': nll/n,
            'aurc': area, 'reliability': bins, 'risk_coverage': curve,
            'class_mean_probability': dict(zip(LABELS, [x/n for x in means])),
            'predicted_counts': dict(predicted), 'gold_counts': dict(gold),
            'class_count_shift': {label: predicted[label]-gold[label] for label in LABELS}}


def fit_temperature(cases, rows):
    def loss(log_t):
        return distribution_metrics(cases, rows, math.exp(log_t))['nll']
    lo, hi = -5., 5.
    ratio = (math.sqrt(5)-1)/2
    x, y = hi-ratio*(hi-lo), lo+ratio*(hi-lo)
    fx, fy = loss(x), loss(y)
    if fx is None:
        raise ValueError('No calibration probabilities')
    for _ in range(70):
        if fx < fy:
            hi, y, fy = y, x, fx
            x = hi-ratio*(hi-lo)
            fx = loss(x)
        else:
            lo, x, fx = x, y, fy
            y = lo+ratio*(hi-lo)
            fy = loss(y)
    return math.exp((lo+hi)/2)


def fit_thresholds(cases, rows, temperature):
    indexed = {r['id']: r for r in rows}
    points = []
    for c in cases:
        row = indexed.get(c['id'], {})
        if row.get('error') or row.get('logits') is None:
            continue
        p = probabilities(row['logits'], temperature)
        winners = sorted(range(7), key=p.__getitem__, reverse=True)
        label = LABELS[winners[0]]
        points.append((p[winners[0]], p[winners[0]]-p[winners[1]], label, label == c['expected_intent']))
    wrong_auto = [p for p, _, label, correct in points if label == 'AUTO_RESEARCH' and not correct]
    auto = math.nextafter(max(wrong_auto), math.inf) if wrong_auto else 0.
    if auto > 1:
        auto = None
    best, candidates = None, []
    for threshold in sorted({0., 1., *(p for p, _, _, _ in points)}):
        for margin in sorted({0., 1., *(m for _, m, _, _ in points)}):
            accepted = [(p, m, label, correct) for p, m, label, correct in points
                        if p >= threshold and m >= margin and (label != 'AUTO_RESEARCH' or auto is not None and p >= auto)]
            errors = sum(not correct for _, _, _, correct in accepted)
            count = len(accepted)
            point = {'tau_intent': threshold, 'tau_margin': margin, 'tau_auto': auto,
                     'accepted': count, 'errors': errors, 'coverage': count/len(cases), 'risk': errors/count if count else 0.}
            candidates.append(point)
            if errors <= .02 * count and (best is None or (count, -errors, -threshold, -margin) >
                                          (best['accepted'], -best['errors'], -best['tau_intent'], -best['tau_margin'])):
                best = point
    # At p=1/margin=1 even an exactly-one wrong class may remain. Explicit reject-all.
    if best is None:
        best = {'tau_intent': 1., 'tau_margin': 1., 'tau_auto': None, 'accepted': 0, 'errors': 0, 'coverage': 0., 'risk': 0., 'reject_all': True}
    return best, candidates


def paired_comparison(cases, first, second, draws=10000, seed=17):
    def correctness(rows):
        by_id = {r['id']: r for r in rows}
        return [int(not by_id.get(c['id'], {}).get('error') and
                    (by_id.get(c['id'], {}).get('response') or {}).get('intent') == c['expected_intent']) for c in cases]
    a, b = correctness(first), correctness(second)
    gain = sum(x and not y for x, y in zip(a, b))
    loss = sum(y and not x for x, y in zip(a, b))
    discordant = gain + loss
    p = min(1., 2 * sum(math.comb(discordant, k) for k in range(min(gain, loss)+1)) / 2**discordant) if discordant else 1.
    delta = [x-y for x, y in zip(a, b)]
    rng = random.Random(seed)
    samples = sorted(sum(rng.choices(delta, k=len(delta)))/len(delta) for _ in range(draws))
    return {'n': len(cases), 'accuracy_difference_first_minus_second': sum(delta)/len(delta),
            'first_only_correct': gain, 'second_only_correct': loss, 'mcnemar_exact_two_sided_p': p,
            'bootstrap_draws': draws, 'bootstrap_seed': seed, 'paired_bootstrap_95': [samples[int(.025*draws)], samples[int(.975*draws)-1]],
            'limitation': 'Agent-authored/template-related cases are correlated; intervals are descriptive, not population guarantees.'}


def diagrams(metrics, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    bins = [b for b in metrics['reliability'] if b['n']]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), layout='constrained')
    axes[0].plot([0, 1], [0, 1], '--', color='gray')
    axes[0].scatter([b['confidence'] for b in bins], [b['accuracy'] for b in bins], s=[20+3*b['n'] for b in bins])
    axes[0].set(xlim=(0, 1), ylim=(0, 1), xlabel='Mean confidence', ylabel='Accuracy', title='Reliability (15 equal-width bins)')
    points = metrics['risk_coverage']
    axes[1].step([0.] + [p['coverage'] for p in points], [0.] + [p['risk'] for p in points], where='pre')
    axes[1].set(xlim=(0, 1), ylim=(0, 1), xlabel='Coverage', ylabel='Error risk', title='Risk–coverage')
    fig.savefig(Path(out)/'calibration.png', dpi=160)
    plt.close(fig)
    write(Path(out)/'calibration-plot-data.json', {'reliability': metrics['reliability'], 'risk_coverage': points})


def calibrate(run, out):
    protocol = read(run/'protocol.json')
    panel = verify_frozen()
    if protocol['split'] != 'dev_v2' or protocol['panel_sha256'] != sha(ROOT/'datasets/intent_routing/v2.json'):
        raise ValueError('Calibration requires the frozen dev_v2 run')
    cases = [c for c in panel['cases'] if c['split'] == 'dev_v2']
    rows = read(run/'results.json')
    if {r['id'] for r in rows} != {c['id'] for c in cases} or len(rows) != len(cases) or any(r.get('error') or r.get('logits') is None for r in rows):
        raise ValueError('Complete successful dev probabilities required')
    out.mkdir(parents=True, exist_ok=False)
    temperature = fit_temperature(cases, rows)
    thresholds, grid = fit_thresholds(cases, rows, temperature)
    def portable(value):
        if value is None:
            return None
        path = Path(value).resolve()
        return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    config = {'schema_version': 1, 'calibrated': True, 'model': protocol['model'], 'model_path': portable(protocol['model_path']),
              'adapter_path': portable(protocol.get('adapter_path')), 'weights_sha256': protocol['weights_sha256'],
              'adapter_sha256': protocol.get('adapter_sha256'), 'prompt_sha256': protocol['prompt_sha256'],
              'temperature': temperature, 'order': protocol['order'], 'max_chars': protocol['max_chars'],
              'score_timeout_seconds': 30, **{k: thresholds[k] for k in ('tau_intent', 'tau_margin', 'tau_auto')},
              'reject_all': thresholds.get('reject_all', False), 'auto_research_policy': 'llm_confirm', 'calibration_split': 'dev_v2',
              'panel_sha256': protocol['panel_sha256'], 'calibration_run': str(run), 'calibration_results_sha256': sha(run/'results.json')}
    metrics = distribution_metrics(cases, rows, temperature)
    write(out/'config.json', config)
    write(out/'selection.json', {'before': distribution_metrics(cases, rows), 'after': metrics, 'selected': thresholds,
                                 'threshold_grid': grid, 'classification': score(cases, rows)})
    diagrams(metrics, out)
    print(json.dumps({'temperature': temperature, 'thresholds': thresholds, 'ece': metrics['ece'], 'config': str(out/'config.json')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    calibrate(args.run, args.out)
