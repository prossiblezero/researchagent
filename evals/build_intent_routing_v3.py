"""Audit independent v3 author/reviewer input against v1/v2 and freeze 160 cases."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent.decision_router import LABELS, digest, weight_identity
from evals.build_intent_routing_v2 import read, write, write_atomic, sha, normalized, grams, jaccard, text, verify_frozen as verify_v2


def cases_hash(cases):
    return digest(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode())


def validate(panel, *, reviewed=True):
    cases = panel['cases']
    if len(cases) != 160 or len({c['id'] for c in cases}) != 160:
        raise ValueError('Exactly 160 unique cases required')
    if Counter(c['part'] for c in cases) != {'regular': 105, 'auto_boundary': 35, 'colloquial': 20}:
        raise ValueError('Invalid v3 part counts')
    if Counter(c['expected_intent'] for c in cases if c['part'] == 'regular') != dict.fromkeys(LABELS, 15):
        raise ValueError('Regular part requires 15 per intent')
    boundaries = Counter(c.get('boundary_kind') for c in cases if c['part'] == 'auto_boundary')
    if boundaries != {'missing_topic': 15, 'resolvable_topic': 10, 'research_only': 10}:
        raise ValueError('Invalid AUTO boundary counts')
    inputs = set()
    for c in cases:
        if (not isinstance(c.get('origin'), dict) or not isinstance(c['origin'].get('kind'), str)
                or not c['origin']['kind'] or not isinstance(c.get('tags'), list) or not c['tags']
                or any(not isinstance(t, str) or not t for t in c['tags'])
                or not isinstance(c.get('space'), dict)
                or any(not isinstance(c['space'].get(k), str) for k in ('name', 'description'))):
            raise ValueError('Invalid scoring or routing metadata')
        if c['split'] != 'test_v3' or c['expected_intent'] not in LABELS or not c['rationale'] or not c['tags']:
            raise ValueError('Invalid case metadata: ' + c['id'])
        if not c['id'].startswith('v3-') or any(ch not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for ch in c['id']):
            raise ValueError('Unsafe case ID')
        if c['search_scope'] not in {'auto', 'web', 'arxiv'}:
            raise ValueError('Invalid search scope')
        if not c['history'] or c['history'][-1]['role'] != 'user' or any(
                set(m) != {'role', 'content'} or m['role'] not in {'user', 'assistant'}
                or not isinstance(m['content'], str) or not m['content'].strip() for m in c['history']):
            raise ValueError('Invalid conversation')
        if not isinstance(c['space'].get('local_material_count'), int) or c['space']['local_material_count'] < 0:
            raise ValueError('Invalid local material count')
        key = normalized(c)
        if key in inputs:
            raise ValueError('Duplicate v3 conversation')
        inputs.add(key)
        annotation = c['annotation']
        if not annotation.get('generator') or any(annotation.get(k) is not False
                for k in ('model_predictions_read', 'prior_dataset_examples_read', 'human_annotated')):
            raise ValueError('Blind independent agent provenance required')
        if reviewed and (not annotation.get('reviewer') or annotation['reviewer'] == annotation['generator']):
            raise ValueError('Independent review required')
        kind = c.get('boundary_kind')
        allowed = {'missing_topic': {'CHAT'}, 'resolvable_topic': {'AUTO_RESEARCH'}, 'research_only': {'CHAT', 'RESEARCH'}}
        if c['part'] == 'auto_boundary' and c['expected_intent'] not in allowed[kind]:
            raise ValueError('Boundary label contradicts policy')
    for label in LABELS:
        scopes = Counter(c['search_scope'] for c in cases if c['expected_intent'] == label)
        values = [scopes[s] for s in ('auto', 'web', 'arxiv')]
        if max(values)-min(values) > 1:
            raise ValueError('Search scope imbalance for ' + label)
    if reviewed:
        audit = panel['label_audit']
        if (audit.get('model_predictions_read') is not False or audit.get('human_annotated') is not False
                or audit.get('gold_labels_visible') is not True or not audit.get('reviewer')
                or audit.get('reviewed_cases_sha256') != cases_hash(cases)
                or {r['id'] for r in audit['case_reviews']} != {c['id'] for c in cases}
                or len(audit['case_reviews']) != 160):
            raise ValueError('Complete prediction-blind review receipt required')


def audit(candidate, out, encoder_path, device):
    if out.exists():
        raise FileExistsError(out)
    panel = read(candidate)
    validate(panel, reviewed=False)
    v2 = verify_v2()
    v1_path, v2_path = ROOT/'datasets/intent_routing/v1.json', ROOT/'datasets/intent_routing/v2.json'
    reference = read(v1_path)['cases'] + v2['cases']
    cases = panel['cases']
    comparisons = reference + cases
    values = [normalized(c) for c in comparisons]
    bags = [grams(v) for v in values]
    conflicts = []
    for i, c in enumerate(cases):
        k = len(reference)+i
        for j in range(k):
            overlap = jaccard(bags[k], bags[j])
            if values[k] == values[j] or overlap >= .6:
                conflicts.append({'id': c['id'], 'method': 'exact' if values[k] == values[j] else 'char5_jaccard',
                                  'reference_id': comparisons[j]['id'], 'similarity': overlap})
    from sentence_transformers import SentenceTransformer
    encoder = SentenceTransformer(str(encoder_path), device=device, local_files_only=True)
    encoder.max_seq_length = 512
    vectors = encoder.encode([text(c) for c in comparisons], batch_size=16, normalize_embeddings=True, show_progress_bar=True)
    scores = vectors[len(reference):] @ vectors.T
    maximum = 0.
    for i, c in enumerate(cases):
        row = scores[i, :len(reference)+i]
        j = int(row.argmax())
        maximum = max(maximum, float(row[j]))
        if float(row[j]) >= .92:
            conflicts.append({'id': c['id'], 'method': 'bge_cosine', 'reference_id': comparisons[j]['id'], 'similarity': float(row[j])})
    result = {'candidate_sha256': sha(candidate), 'cases_sha256': cases_hash(cases),
              'reference_sha256': {'v1': sha(v1_path), 'v2': sha(v2_path)}, 'reference_cases': len(reference),
              'normalization': 'NFKC/casefold/whitespace removal on role+conversation; metadata excluded',
              'exact': True, 'jaccard_ngram': 5, 'jaccard_threshold': .6,
              'semantic': {'completed': True, 'model': 'BAAI/bge-m3', 'weights_sha256': weight_identity(encoder_path),
                           'cosine_threshold': .92, 'max_tokens': 512, 'max_cosine': maximum,
                           'scope': 'v3 vs all v1/v2, plus within-v3'},
              'conflicts': conflicts, 'passed': not conflicts, 'completed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write(out, result)
    print(json.dumps({'audit': str(out), 'passed': result['passed'], 'max_cosine': maximum, 'conflicts': conflicts}, ensure_ascii=False))


def freeze(reviewed, audit_path, destination):
    lock = ROOT/'datasets/.intent-routing-v3.freeze.lock'
    lock.touch(exist_ok=False)
    try:
        if destination.exists():
            raise FileExistsError(destination)
        panel, dedup = read(reviewed), read(audit_path)
        validate(panel)
        if not dedup['passed'] or dedup['conflicts'] or dedup['candidate_sha256'] != sha(reviewed) or dedup['cases_sha256'] != cases_hash(panel['cases']):
            raise ValueError('Successful dedup must cover final reviewed bytes')
        for version, expected in dedup['reference_sha256'].items():
            if expected != sha(ROOT/f'datasets/intent_routing/{version}.json'):
                raise ValueError('Dedup reference changed')
        manifest_path = ROOT/'datasets/manifest.json'
        manifest = read(manifest_path)
        if any(d['id'] == 'intent-routing-v3' for d in manifest['datasets']):
            raise ValueError('v3 already registered')
        if destination.resolve() != (ROOT/'datasets/intent_routing/v3.json').resolve():
            raise ValueError('Freeze destination must match manifest path')
        panel.update(status='frozen', frozen_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                     intents=list(LABELS), label_policy=read(ROOT/'datasets/intent_routing/v1.json')['label_policy'],
                     dedup={**dedup, 'removed': panel.get('dedup_removals', []), 'removed_count': len(panel.get('dedup_removals', []))})
        write_atomic(destination, panel)
        entry = {'id': 'intent-routing-v3', 'path': 'datasets/intent_routing/v3.json', 'kind': 'intent_routing',
                 'provenance': 'independent_blind_llm_agents_not_human_annotation', 'status': 'frozen',
                 'sha256': sha(destination), 'case_count': 160, 'split': {'test_v3': 160},
                 'parts': dict(Counter(c['part'] for c in panel['cases']))}
        manifest['datasets'].append(entry)
        write_atomic(manifest_path, manifest, replace=True)
        with (ROOT/'datasets/README.md').open('a', encoding='utf-8', newline='\n') as stream:
            stream.write('\n\n2026-10-18 新增独立保留集 v3：`intent_routing/v3.json`，只含 test_v3 160 题'
                         '（常规105、自动研究边界35、口语20）。独立盲态代理编写与复核，非人工标注；'
                         '相对 v1/v2 全集去重，scope 在每类内均衡。SHA-256：`'+entry['sha256']+'`。每个预声明组只运行一次。\n')
        print(json.dumps(entry, ensure_ascii=False))
    finally:
        lock.unlink()

def verify_frozen(path=ROOT/'datasets/intent_routing/v3.json'):
    panel = read(path)
    validate(panel)
    entry = next(d for d in read(ROOT/'datasets/manifest.json')['datasets'] if d['id'] == 'intent-routing-v3')
    if (panel.get('status') != 'frozen' or sha(path) != entry['sha256'] or not panel['dedup']['passed']
            or panel['dedup']['cases_sha256'] != cases_hash(panel['cases'])):
        raise ValueError('Frozen v3 integrity mismatch')
    return panel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('audit')
    check.add_argument('--candidate', type=Path, required=True)
    check.add_argument('--out', type=Path, required=True)
    check.add_argument('--encoder', type=Path, default=ROOT/'data/models/bge-m3')
    check.add_argument('--device', default='cuda')
    lock = sub.add_parser('freeze')
    lock.add_argument('--reviewed', type=Path, required=True)
    lock.add_argument('--audit', type=Path, required=True)
    lock.add_argument('--out', type=Path, default=ROOT/'datasets/intent_routing/v3.json')
    args = parser.parse_args()
    if args.command == 'audit':
        audit(args.candidate, args.out, args.encoder, args.device)
    else:
        freeze(args.reviewed, args.audit, args.out)


if __name__ == '__main__':
    main()
