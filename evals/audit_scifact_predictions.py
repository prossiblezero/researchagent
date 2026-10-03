"""Independently score all official dev claims; never import generated method code."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

LABELS = ('SUPPORT', 'CONTRADICT', 'NOINFO')


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def score_predictions(claims, predictions):
    gold = {}
    for claim in claims:
        identifier = claim['id']
        if type(identifier) is not int or identifier in gold:
            raise ValueError('Claim IDs must be unique integers')
        labels = {item['label'] for group in claim.get('evidence', {}).values() for item in group}
        if labels - set(LABELS[:2]) or len(labels) > 1:
            raise ValueError('Unexpected or conflicting official claim labels')
        gold[identifier] = next(iter(labels)) if labels else 'NOINFO'
    guessed = {}
    for row in predictions:
        identifier = row['id']
        if type(identifier) is not int or identifier not in gold or identifier in guessed:
            raise ValueError('Prediction IDs must match official claims exactly once')
        if row['prediction'] not in LABELS:
            raise ValueError('Unknown prediction label')
        if 'gold' in row and row['gold'] != gold[identifier]:
            raise ValueError('Saved gold disagrees with original official evidence')
        guessed[identifier] = row['prediction']
    if not gold or guessed.keys() != gold.keys():
        raise ValueError('Predictions must cover every official claim')
    matrix = [[0] * len(LABELS) for _ in LABELS]
    for identifier, label in gold.items():
        matrix[LABELS.index(label)][LABELS.index(guessed[identifier])] += 1
    f1s = {}
    for i, label in enumerate(LABELS):
        tp = matrix[i][i]
        denominator = sum(matrix[i]) + sum(row[i] for row in matrix)
        f1s['f1_' + label.lower()] = 2 * tp / denominator if denominator else 0.0
    return {'metrics': {'accuracy': sum(matrix[i][i] for i in range(3)) / len(gold),
                       'macro_f1': sum(f1s.values()) / len(LABELS), **f1s, 'num_examples': len(gold)},
            'diagnostics': {'labels': list(LABELS), 'confusion_matrix': matrix,
                            'errors': [{'id': i, 'gold': gold[i], 'prediction': guessed[i]}
                                       for i in gold if gold[i] != guessed[i]]}}


def audit(claims_path, predictions_path, result_path, expected_claims_sha256):
    inputs = {name: {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
              for name, path in [('claims', claims_path), ('predictions', predictions_path), ('result', result_path)]}
    if inputs['claims']['sha256'] != expected_claims_sha256:
        raise ValueError('Official claims hash differs from the dataset catalog')
    scored = score_predictions(read_jsonl(claims_path), read_jsonl(predictions_path))
    reported = json.loads(result_path.read_text(encoding='utf-8'))
    differences = {}
    for key, expected in scored['metrics'].items():
        value = reported.get('metrics', {}).get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not math.isclose(value, expected, abs_tol=1e-9):
            differences[key] = {'reported': value, 'independent': expected}
    matrix = reported.get('diagnostics', {}).get('confusion_matrix', reported.get('metrics', {}).get('confusion_matrix'))
    if matrix != scored['diagnostics']['confusion_matrix']:
        differences['confusion_matrix'] = {'reported': matrix, 'independent': scored['diagnostics']['confusion_matrix']}
    return {'inputs': inputs, **scored, 'differences': differences, 'scores_reproduced': not differences,
            'scope': 'Scoring and sample coverage only; does not certify training isolation, timing, scientific novelty or tool completion.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claims', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    catalog = json.loads((Path(__file__).resolve().parents[1] / 'datasets/open/catalog.json').read_text(encoding='utf-8'))
    if args.output.exists():
        parser.error('Preserve prior audits; select a new output file')
    result = audit(args.claims, args.predictions, args.result, catalog['scifact']['raw_sha256']['claims_dev.jsonl'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'scores_reproduced': result['scores_reproduced'], 'metrics': result['metrics'], 'output': str(args.output)}))
    return 0 if result['scores_reproduced'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
