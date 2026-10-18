"""Assemble independently authored data, deduplicate against held-out/v1, then freeze.

No classifier runs here. The existing BGE-M3 is used only for the prescribed
leakage check. Frozen files are never overwritten.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent.decision_router import LABELS, weight_identity

V2_PUBLIC_EXCLUSIONS = {'5245': 'Independent pre-run review: notification setup, not a new-facts query.'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def write_atomic(path, value, *, replace=False):
    """Publish complete JSON, rejecting an existing destination unless explicitly replacing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, suffix='.tmp', encoding='utf-8', newline='\n', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text(case):
    # Compare the actual conversation, not labels, source IDs or boilerplate space names.
    return '\n'.join(m['role'] + ': ' + m['content'] for m in case['history'])


def normalized(case):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text(case)).casefold())


def grams(value):
    return {value[i:i+5] for i in range(max(1, len(value)-4))}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 1.0


def validate(panel, *, frozen=True):
    if panel['intents'] != list(LABELS) or panel['label_policy'] != read(ROOT/'datasets/intent_routing/v1.json')['label_policy']:
        raise ValueError('Label policy drift')
    ids, groups, inputs, origins = set(), {}, {}, set()
    counts = Counter()
    for c in panel['cases']:
        if c['id'] in ids or c['split'] not in {'train_v2', 'dev_v2', 'test_v2'} or c['expected_intent'] not in LABELS:
            raise ValueError('Duplicate ID or invalid split/label')
        ids.add(c['id'])
        if groups.setdefault(c['group'], c['split']) != c['split']:
            raise ValueError('Scenario group crosses splits')
        key = normalized(c)
        if key in inputs:
            raise ValueError('Duplicate conversation: ' + c['id'] + ' / ' + inputs[key])
        inputs[key] = c['id']
        if not c['history'] or c['history'][-1]['role'] != 'user' or not c['rationale']:
            raise ValueError('Missing user turn/rationale')
        if any(set(m) != {'role', 'content'} or m['role'] not in {'user', 'assistant'}
               or not isinstance(m['content'], str) or not m['content'].strip() for m in c['history']):
            raise ValueError('Invalid history')
        if c['split'] == 'dev_v2' and 'adversarial' in c['tags']:
            raise ValueError('Adversarial augmentation belongs only to train/test')
        annotation = c['annotation']
        if not annotation['generator'] or (frozen and (not annotation['reviewer'] or annotation['reviewer'] == annotation['generator'])):
            raise ValueError('Independent agent provenance/review required')
        if c['origin']['kind'] == 'MASSIVE':
            if c['split'] == 'train_v2' or c['origin']['id'] in origins:
                raise ValueError('MASSIVE source ID overlap')
            origins.add(c['origin']['id'])
        counts[c['split'], c['expected_intent']] += 1
    for label in LABELS:
        if not 120 <= counts['train_v2', label] <= 200 or counts['dev_v2', label] != 10 or counts['test_v2', label] != 20:
            raise ValueError('Invalid split counts: ' + str(dict(counts)))
    if frozen:
        if panel.get('status') != 'frozen' or not panel.get('dedup', {}).get('semantic', {}).get('completed'):
            raise ValueError('Semantic dedup and freeze required')
    return counts


def verify_frozen(path=ROOT/'datasets/intent_routing/v2.json'):
    panel = read(path)
    validate(panel)
    entry = next(d for d in read(ROOT/'datasets/manifest.json')['datasets'] if d['id'] == 'intent-routing-v2')
    if entry['sha256'] != sha(path):
        raise ValueError('Frozen v2 hash mismatch')
    return panel


def massive_cases(source_dir):
    v1 = read(ROOT/'datasets/intent_routing/v1.json')
    sources = {lang: read(source_dir/f'massive-{short}-test.json') for lang, short in [('zh-CN', 'zh'), ('en-US', 'en')]}
    indexes = {lang: {str(r['id']): r for r in rows} for lang, rows in sources.items()}
    used = {str(c['origin']['id']) for c in v1['cases'] if c['origin']['kind'] == 'MASSIVE'}
    excluded = set(v1['selection']['exclusions']) | set(V2_PUBLIC_EXCLUSIONS)
    mapping = {}
    for c in v1['cases']:
        if c['origin']['kind'] == 'MASSIVE':
            mapping[indexes[c['origin']['locale']][str(c['origin']['id'])]['intent']] = (c['expected_intent'], c['origin']['original_intent'])
    result = []
    for label in ('CHAT', 'RESEARCH'):
        pool = [r for r in sources['en-US'] if r['intent'] in mapping and mapping[r['intent']][0] == label
                and str(r['id']) not in used | excluded]
        pool.sort(key=lambda r: hashlib.sha256(('intent-routing-v2:20261017:' + str(r['id'])).encode()).hexdigest())
        for i, row in enumerate(pool[:6]):
            locale = 'zh-CN' if i % 2 == 0 else 'en-US'
            original = indexes[locale][str(row['id'])]
            split = 'dev_v2' if i < 2 else 'test_v2'
            result.append({'id': f'{split}-massive-{row["id"]}', 'split': split, 'group': f'massive-{row["id"]}',
                'expected_intent': label, 'rationale': v1['label_policy'][label],
                'origin': {'kind': 'MASSIVE', 'dataset_version': '1.1', 'official_split': 'test', 'id': str(row['id']),
                           'locale': locale, 'original_intent': mapping[row['intent']][1], 'license': 'CC-BY-4.0',
                           'adaptation': 'utterance unchanged; taxonomy mapping only'},
                'annotation': {'generator': 'Amazon-MASSIVE/deterministic-v2-selection', 'reviewer': None},
                'tags': ['public', 'zh' if locale == 'zh-CN' else 'en'],
                'history': [{'role': 'user', 'content': original['utt']}],
                'space': {'name': '公开评测工作区', 'description': '论文调研、已导入资料与实验方案讨论', 'local_material_count': 4},
                'search_scope': 'auto'})
    return result


def assemble(train_path, heldout_path, sources, out, encoder_path, device):
    if out.exists():
        raise FileExistsError(out)
    v1 = read(ROOT/'datasets/intent_routing/v1.json')
    train, heldout = read(train_path)['cases'], read(heldout_path)['cases'] + massive_cases(sources)
    reference = heldout + v1['cases']
    references = [(c['id'], normalized(c), grams(normalized(c))) for c in reference]
    removed, candidates, seen = [], [], set()
    for c in train:
        value = normalized(c)
        bag = grams(value)
        duplicate = next((key for key, other, g in references if value == other or jaccard(bag, g) >= .6), None)
        if duplicate is not None or value in seen:
            removed.append({'id': c['id'], 'method': 'exact_or_char5_jaccard', 'reference_id': duplicate})
            continue
        seen.add(value)
        candidates.append(c)
    from sentence_transformers import SentenceTransformer
    encoder = SentenceTransformer(str(encoder_path), device=device, local_files_only=True)
    encoder.max_seq_length = 512
    ref_vectors = encoder.encode([text(c) for c in reference], batch_size=16, normalize_embeddings=True, show_progress_bar=True)
    vectors = encoder.encode([text(c) for c in candidates], batch_size=16, normalize_embeddings=True, show_progress_bar=True)
    similarities = vectors @ ref_vectors.T
    kept, counts = [], Counter()
    for c, scores in zip(candidates, similarities):
        i = int(scores.argmax())
        if float(scores[i]) > .92:
            removed.append({'id': c['id'], 'method': 'bge_cosine', 'reference_id': reference[i]['id'], 'similarity': float(scores[i])})
        elif counts[c['expected_intent']] < 200:
            kept.append(c)
            counts[c['expected_intent']] += 1
        else:
            removed.append({'id': c['id'], 'method': 'predeclared_max_200_per_class'})
    panel = {'schema_version': 2, 'name': 'ResearchAgent intent routing v2', 'created_at': '2026-10-17', 'status': 'pending_independent_review',
        'intents': list(LABELS), 'label_policy': v1['label_policy'],
        'provenance': {**v1['provenance'], 'selection': 'Unused source IDs, same taxonomy/exclusions, SHA256 intent-routing-v2:20261017:<id>, six per mapped class; first two dev, rest test; alternating zh/en.',
            'additional_public_exclusions': V2_PUBLIC_EXCLUSIONS,
            'author_files_sha256': {Path(p).name: sha(p) for p in (train_path, heldout_path)},
            'raw_sources_sha256': {p.name: sha(p) for p in sources.glob('massive-*-test.json')}},
        'dedup': {'normalization': 'NFKC/casefold/whitespace removal on role+conversation; metadata excluded',
                  'exact': True, 'jaccard_ngram': 5, 'jaccard_threshold': .6,
                  'semantic': {'completed': True, 'model': 'BAAI/bge-m3', 'weights_sha256': weight_identity(encoder_path),
                               'cosine_threshold': .92, 'max_tokens': 512, 'scope': 'train vs dev/test/v1, not within-train semantic diversity'},
                  'input_train_count': len(train), 'removed_count': len(removed), 'removed': removed},
        'limits': ['LLM-agent-authored and independently agent-reviewed, not human annotation.',
                   'Template/topic correlations remain; intervals are descriptive.', 'MASSIVE subset adaptation, not official benchmark accuracy.',
                   'v1 is a seen regression set. v2 test is held out and each comparison group runs once.'],
        'cases': kept + heldout}
    write(out, panel)
    print(json.dumps({'train_counts': dict(counts), 'removed': len(removed), 'review_input': str(out)}, ensure_ascii=False))
    validate(panel, frozen=False)


def audit_reviewed(panel, encoder_path, device):
    """Review edits precede freeze, so check the final reviewed bytes as well."""
    train = [c for c in panel['cases'] if c['split'] == 'train_v2']
    reference = [c for c in panel['cases'] if c['split'] != 'train_v2'] + read(ROOT/'datasets/intent_routing/v1.json')['cases']
    ref_grams = [(c['id'], normalized(c), grams(normalized(c))) for c in reference]
    for c in train:
        value, bag = normalized(c), grams(normalized(c))
        if any(value == other or jaccard(bag, g) >= .6 for _, other, g in ref_grams):
            raise ValueError('Review introduced lexical leakage: ' + c['id'])
    from sentence_transformers import SentenceTransformer
    encoder = SentenceTransformer(str(encoder_path), device=device, local_files_only=True)
    encoder.max_seq_length = 512
    vectors = encoder.encode([text(c) for c in train+reference], batch_size=16, normalize_embeddings=True)
    scores = vectors[:len(train)] @ vectors[len(train):].T
    if float(scores.max()) > .92:
        raise ValueError('Review introduced semantic leakage; rebuild/review before freeze')
    return {'completed': True, 'reviewed_conversations_sha256': hashlib.sha256(json.dumps(
        [c['history'] for c in panel['cases']], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        'max_train_reference_cosine': float(scores.max()), 'reference_cases': len(reference), 'train_cases': len(train)}


def freeze(reviewed, destination, encoder_path, device):
    if destination.exists():
        raise FileExistsError(destination)
    panel = read(reviewed)
    panel['status'] = 'frozen'
    validate(panel)
    manifest_path = ROOT/'datasets/manifest.json'
    manifest = read(manifest_path)
    if any(d['id'] == 'intent-routing-v2' for d in manifest['datasets']):
        raise ValueError('v2 manifest already registered')
    panel['dedup']['post_review_check'] = audit_reviewed(panel, encoder_path, device)
    write(destination, panel)
    counts = Counter(c['split'] for c in panel['cases'])
    entry = {'id': 'intent-routing-v2', 'path': 'datasets/intent_routing/v2.json', 'kind': 'intent_routing',
             'provenance': 'independent_llm_agents_plus_unused_massive_subset', 'status': 'frozen',
             'sha256': sha(destination), 'case_count': len(panel['cases']), 'split': dict(counts)}
    manifest['datasets'].append(entry)
    tokenizer_path = ROOT/'datasets/intent_routing/qwen3-tokenization.json'
    manifest['datasets'].append({'id': 'decision-router-qwen3-tokenization', 'path': tokenizer_path.relative_to(ROOT).as_posix(),
        'kind': 'tokenizer_contract', 'provenance': 'measured_pinned_public_tokenizer', 'status': 'frozen', 'sha256': sha(tokenizer_path)})
    write(manifest_path, manifest)
    with (ROOT/'datasets/README.md').open('a', encoding='utf-8', newline='\n') as stream:
        stream.write('\n\n2026-10-17 新增决策路由 v2：`intent_routing/v2.json`，' + str(dict(counts)) +
                     '；独立代理编写与复核，非人工标注。SHA-256：`' + entry['sha256'] + '`。v1 仅作回归；v2 test 每组只运行一次。\n')
    print(json.dumps(entry, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('assemble')
    for name in ('train', 'heldout', 'sources', 'out'):
        build.add_argument('--' + name, type=Path, required=True)
    build.add_argument('--encoder', type=Path, default=ROOT/'data/models/bge-m3')
    build.add_argument('--device', default='cuda')
    locked = commands.add_parser('freeze')
    locked.add_argument('--reviewed', type=Path, required=True)
    locked.add_argument('--out', type=Path, default=ROOT/'datasets/intent_routing/v2.json')
    locked.add_argument('--encoder', type=Path, default=ROOT/'data/models/bge-m3')
    locked.add_argument('--device', default='cuda')
    args = parser.parse_args()
    if args.command == 'assemble':
        assemble(args.train, args.heldout, args.sources, args.out, args.encoder, args.device)
    else:
        freeze(args.reviewed, args.out, args.encoder, args.device)


if __name__ == '__main__':
    main()
