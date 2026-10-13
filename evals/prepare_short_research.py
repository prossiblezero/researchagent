"""Freeze new public retrieval subsets before any autonomous comparison runs.

Run with an existing Python containing pyarrow; it is only needed to read raw Parquet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
RAW = Path('D:/paper/researchagent-benchmarks/raw')
LOCOMO = Path('D:/paper/researchagent-sources/AgenticMemory/0c8039f28fdcc08189a23c07a3437d9d2482f9c2/data/locomo10.json')
SEED = 'short-research-v1-20261011'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def ordered(rows):
    return sorted(rows, key=lambda row: digest((SEED + row['id']).encode()))


def qasper():
    import pyarrow.parquet as pq
    excluded = {r['source_id'] for r in read(ROOT/'datasets/open/qasper/tasks.json')}
    result = {}
    for split, raw, count in [('dev', 'qasper_train.parquet', 24), ('held_out', 'qasper_validation.parquet', 48)]:
        candidates = []
        for paper in pq.read_table(RAW/raw).to_pylist():
            if paper['id'] in excluded:
                continue
            full = paper['full_text']
            texts = [(s, t) for s, group in zip(full['section_name'], full['paragraphs']) for t in group]
            texts += [('Abstract', paper['abstract'])]
            texts += [('Caption', t) for t in paper.get('figures_and_tables', {}).get('caption', [])]
            docs = [{'id': f"{paper['id']}:p{i}", 'text': t, 'title': paper['title'], 'section': s}
                    for i, (s, t) in enumerate(texts)]
            lookup = {' '.join(d['text'].split()): d['id'] for d in reversed(docs)}
            qas = paper['qas']
            for question, qid, bundle in zip(qas['question'], qas['question_id'], qas['answers']):
                annotations = bundle['answer']
                quotes = [' '.join(e.split()) for a in annotations for e in a.get('evidence', [])]
                if not quotes or any(e not in lookup for e in quotes) or all(a['unanswerable'] for a in annotations):
                    continue
                candidates.append({'id': qid, 'group': paper['id'], 'query': question,
                                   'documents': docs, 'gold': sorted({lookup[e] for e in quotes})})
        # One question per paper; split at source level, never by observed method scores.
        groups, selected = set(), []
        for row in ordered(candidates):
            if row['group'] not in groups and len(selected) < count:
                groups.add(row['group'])
                selected.append(row)
        assert len(selected) == count
        result[split] = selected
        excluded.update(groups)
    return result


def hotpot():
    import pyarrow.parquet as pq
    excluded = {r['id'].removeprefix('hotpotqa-') for r in read(ROOT/'datasets/open/hotpotqa/tasks.json')}
    candidates = []
    for row in pq.read_table(RAW/'hotpotqa_distractor_validation.parquet').to_pylist():
        if row['id'] in excluded:
            continue
        docs = [{'id': f"{row['id']}:c{i}", 'title': title, 'text': ' '.join(sentences)}
                for i, (title, sentences) in enumerate(zip(row['context']['title'], row['context']['sentences']))]
        gold_titles = set(row['supporting_facts']['title'])
        candidates.append({'id': row['id'], 'group': row['id'], 'query': row['question'],
                           'documents': docs, 'gold': [d['id'] for d in docs if d['title'] in gold_titles]})
    # Source-title disjoint dev/holdout to reduce near-duplicate context leakage.
    dev = ordered(candidates)[:32]
    titles = {d['title'] for c in dev for d in c['documents']}
    held = [c for c in ordered(candidates) if not any(d['title'] in titles for d in c['documents'])][:64]
    assert len(held) == 64
    return {'dev': dev, 'held_out': held}


def locomo():
    assert digest(LOCOMO.read_bytes()) == '79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4'
    samples = {s['sample_id']: s for s in read(LOCOMO)}
    result = {}
    for split, ids in [('dev', ['conv-43', 'conv-44']), ('held_out', ['conv-47', 'conv-48'])]:
        selected = []
        for cid in ids:
            sample = samples[cid]
            docs = []
            for session, turns in sample['conversation'].items():
                if not isinstance(turns, list):
                    continue
                for turn in turns:
                    docs.append({'id': turn['dia_id'], 'title': str(sample['conversation'].get(session+'_date_time', '')),
                                 'text': turn['speaker'] + ': ' + turn['text'] + ' ' + str(turn.get('blip_caption', '')),
                                 'session': session})
            allowed = {d['id'] for d in docs}
            candidates = []
            for i, qa in enumerate(sample['qa']):
                gold = sorted({e.strip() for field in qa['evidence'] for e in re.split(r';\s*', field) if e.strip()})
                if qa['category'] in (1, 2, 3, 4) and gold and set(gold) <= allowed:
                    candidates.append({'id': f'{cid}/qa-{i}', 'group': cid, 'query': qa['question'],
                                       'documents': docs, 'gold': gold})
            selected.extend(ordered(candidates)[:24])
        assert len(selected) == 48
        result[split] = selected
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Preserve prior packets; choose a new output directory')
    source_paths = [RAW/n for n in ('qasper_train.parquet', 'qasper_validation.parquet', 'hotpotqa_distractor_validation.parquet')] + [LOCOMO]
    protocol = {'schema': 'short-research/v1', 'seed': SEED, 'before': '25cdbc755be4679ed86271ec018b47ae2e2f4346',
                'after': '3c3e2f83c96e68d35799fe46170dd7d9e115e9f4',
                'arms': ['before', 'after', 'codex'], 'wall_seconds': 360, 'total_reported_tokens': 400000,
                'model': 'gpt-5.6-luna', 'provider_host': 'api.sudocode.chat', 'minimum_gain': 0.02,
                'goal': 'Valid submission within budget and held-out macro evidence recall improvement >= 0.02 over BM25.',
                'scope': 'Three short CPU retrieval-method research tasks, one screening run per arm; not autonomous science or official benchmark scores.',
                'unknown_usage': 'Not eligible for certified budget pass; never treated as zero cost.',
                'run_order': [['qasper', a] for a in ('before', 'after', 'codex')] +
                             [['hotpotqa', a] for a in ('after', 'codex', 'before')] +
                             [['locomo', a] for a in ('codex', 'before', 'after')],
                'sources': {str(p): digest(p.read_bytes()) for p in source_paths}, 'tasks': {}}
    for task, builder, k in [('qasper', qasper, 5), ('hotpotqa', hotpot, 2), ('locomo', locomo, 5)]:
        splits = builder()
        protocol['tasks'][task] = {'k': k, 'packets': {}, 'ids': {s: [r['id'] for r in rows] for s, rows in splits.items()}}
        for split, cases in splits.items():
            path = args.output / task / (split + '.json')
            write(path, {'task': task, 'version': SEED, 'split': split, 'k': k, 'cases': cases})
            protocol['tasks'][task]['packets'][split] = digest(path.read_bytes())
    write(args.output/'protocol.json', protocol)
    print(json.dumps({task: {split: len(ids) for split, ids in info['ids'].items()} for task, info in protocol['tasks'].items()}))


if __name__ == '__main__':
    main()
