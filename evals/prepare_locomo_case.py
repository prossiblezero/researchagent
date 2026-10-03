"""Freeze an upstream LoCoMo subset for A-MEM experiments, without model calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REVISION = '3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376'
RAW_SHA256 = '79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4'
UPSTREAM = 'https://github.com/snap-research/locomo'
SPLITS = {'development': ['conv-26', 'conv-30'], 'holdout': ['conv-41', 'conv-42']}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def freeze(raw):
    if digest(raw) != RAW_SHA256:
        raise ValueError('Official LoCoMo source bytes changed')
    samples = {s['sample_id']: s for s in json.loads(raw)}
    tasks, labels, conversations = [], [], []
    for split, ids in SPLITS.items():
        for cid in ids:
            sample = samples[cid]
            conversation = sample['conversation']
            turns = {t['dia_id'] for k, v in conversation.items()
                     if k.startswith('session_') and isinstance(v, list) for t in v}
            conversations.append({'conversation_id': cid, 'split': split, 'conversation': conversation})
            # Stratified round robin over hash-ranked official QA IDs, without scoring.
            groups = {cat: sorted([i for i, q in enumerate(sample['qa']) if q['category'] == cat],
                                  key=lambda i: digest(f'locomo-agent-v1:{cid}:{i}'.encode()))
                      for cat in (1, 2, 3, 4)}
            selected = []
            while len(selected) < 20 and any(groups.values()):
                for cat in groups:
                    if groups[cat] and len(selected) < 20:
                        selected.append(groups[cat].pop(0))
            for i in selected:
                qa = sample['qa'][i]
                identity = f'{cid}/qa-{i:03d}'
                tasks.append({'id': identity, 'conversation_id': cid, 'split': split,
                              'official_qa_index': i, 'category': qa['category'], 'question': qa['question']})
                refs = list(dict.fromkeys(r.strip() for field in qa['evidence']
                                         for r in re.split(r';\s*', field) if r.strip()))
                labels.append({'id': identity, 'answer': qa['answer'], 'official_evidence': qa['evidence'],
                               'gold_evidence': refs, 'unmapped_evidence': sorted(set(refs) - turns),
                               'retrieval_scorable': bool(refs) and set(refs) <= turns})
    return tasks, labels, conversations


def main():
    archive = Path(f'D:/paper/researchagent-benchmarks/raw/locomo/locomo-{REVISION}.zip')
    prefix = f'locomo-{REVISION}/'
    out = ROOT / 'datasets/open/locomo'
    with zipfile.ZipFile(archive) as source:
        raw = source.read(prefix + 'data/locomo10.json')
        tasks, labels, conversations = freeze(raw)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'LICENSE.txt').write_bytes(source.read(prefix + 'LICENSE.txt'))
    write(out / 'tasks.json', tasks)
    write(out / 'labels.json', labels)
    write(out / 'corpus.json', conversations)
    metadata = {
        'schema': 'locomo-agent-subset/v1', 'official_url': UPSTREAM, 'revision': REVISION,
        'source_url': f'{UPSTREAM}/blob/{REVISION}/data/locomo10.json', 'raw_sha256': RAW_SHA256,
        'license': 'CC-BY-NC-4.0', 'license_source': f'{UPSTREAM}/blob/{REVISION}/LICENSE.txt',
        'splits': SPLITS, 'task_count': len(tasks), 'questions_per_conversation': 20,
        'selection': 'First four official conversations, split by conversation before experiments; categories 1-4 round robin, each hash-ranked by locomo-agent-v1:conversation_id:qa_index.',
        'category_5': 'Excluded: upstream A-MEM uses the reference answer as an option, a different evaluation protocol.',
        'conversation_content': 'Complete original conversations, including distractor turns, timestamps and image captions; no reference answers in corpus or prediction tasks.',
        'labels_boundary': 'labels.json is for host scoring. Do not pass it to memory building, retrieval or QA generation; holdout outcomes cannot guide method selection.',
        'evidence_mapping': 'Split semicolon-delimited annotations only; retain unknown IDs and exclude those tasks from evidence-recall means, never from answer-quality scores.',
        'execution_status': 'not_run', 'benchmark_claim': 'Project subset, not the full LoCoMo or A-MEM published result.',
    }
    write(out / 'selection.json', metadata)
    manifest_path = ROOT / 'datasets/manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for name, kind, description in [
        ('tasks.json', 'agent_memory_qa', '80 original questions across four complete conversations; 40 development and 40 holdout.'),
        ('labels.json', 'host_scoring_labels', 'Reference answers and evidence mappings, separate from model inputs.'),
        ('corpus.json', 'agent_memory_corpus', 'Four complete original conversations, without QA labels.'),
        ('selection.json', 'dataset_protocol', 'Frozen IDs, upstream commit, license, split and evaluation boundaries.'),
    ]:
        path = out / name
        relative = path.relative_to(ROOT).as_posix()
        entry = {'id': 'locomo-agent-v1-' + path.stem, 'path': relative, 'kind': kind,
                 'provenance': 'official_benchmark_fixed_subset', 'status': 'prepared_not_run',
                 'description': description, 'external_benchmark': 'LoCoMo',
                 'license': 'CC-BY-NC-4.0', 'sha256': digest(path.read_bytes())}
        manifest['datasets'] = [e for e in manifest['datasets'] if e['path'] != relative] + [entry]
    registry = manifest['open_source_benchmark_registry']
    registry[:] = [entry for entry in registry if entry['name'] != 'LoCoMo']
    registry.append({'name': 'LoCoMo', 'official_url': UPSTREAM, 'role': 'benchmark_tasks',
                     'execution_status': 'not_run', 'note': metadata['benchmark_claim'] + ' 80 frozen tasks prepared for A-MEM; no scores yet.'})
    write(manifest_path, manifest)
    print(json.dumps({'tasks': len(tasks), 'conversations': len(conversations),
                      'retrieval_scorable': sum(row['retrieval_scorable'] for row in labels),
                      'splits': SPLITS}, ensure_ascii=False))


if __name__ == '__main__':
    main()
