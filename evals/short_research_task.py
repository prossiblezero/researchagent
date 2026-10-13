"""Frozen, dependency-free retrieval task runtime; also used by the host scorer."""
from __future__ import annotations

import argparse
from collections import Counter
import importlib
import json
import math
from pathlib import Path
import re


def rank(query, documents, k):
    """BM25 reference: lowercase alphanumeric tokens, k1=1.2, b=0.75."""
    terms = [Counter(re.findall(r'[a-z0-9]+', d['text'].lower())) for d in documents]
    lengths = [sum(t.values()) for t in terms]
    average = sum(lengths) / max(1, len(lengths)) or 1
    frequencies = Counter(term for row in terms for term in row)
    scores = []
    for i, row in enumerate(terms):
        score = 0.0
        for term in set(re.findall(r'[a-z0-9]+', query.lower())):
            tf = row[term]
            idf = math.log(1 + (len(terms) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[i] / average))
        scores.append(score)
    return [documents[i]['id'] for i in sorted(range(len(documents)), key=lambda i: (-scores[i], documents[i]['id']))[:k]]


def score(cases, predictions, k):
    if not cases or not isinstance(predictions, dict) or set(predictions) != {c['id'] for c in cases}:
        raise ValueError('Predictions must cover every case exactly once')
    values = []
    for case in cases:
        ids = predictions[case['id']]
        allowed = {d['id'] for d in case['documents']}
        if (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)
                or len(ids) != min(k, len(allowed)) or len(set(ids)) != len(ids) or not set(ids) <= allowed):
            raise ValueError('Invalid ranking for ' + case['id'])
        gold = set(case['gold'])
        if not gold or not gold <= allowed:
            raise ValueError('Invalid host labels')
        values.append(len(set(ids) & gold) / len(gold))
    return {'recall_at_k': sum(values) / len(values), 'per_case': values}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method', default='method')
    parser.add_argument('--input', default='dev.json')
    parser.add_argument('--output', default='result.json')
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text(encoding='utf-8'))
    fn = rank if args.method == 'baseline' else importlib.import_module(args.method).rank
    predictions = {c['id']: fn(c['query'], c['documents'], data['k']) for c in data['cases']}
    metrics = score(data['cases'], predictions, data['k']) if all('gold' in c for c in data['cases']) else {}
    result = {'metrics': {key: value for key, value in metrics.items() if key != 'per_case'},
              'predictions': predictions, 'diagnostics': {'per_case': metrics.get('per_case', [])},
              'config': {'dataset': data['task'], 'dataset_version': data['version'], 'split': data['split'],
                         'seeds': [0], 'parameters': {'method': args.method, 'k': data['k']}}}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(result['metrics']))


if __name__ == '__main__':
    main()
