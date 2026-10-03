"""A-MEM research campaign using the product controller, CodingTool and host scorer."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_auto_research_acceptance import campaign
from evals.run_evidence_qa_acceptance import read, sha, write
from research_agent.coding_tool import regular_file
from research_agent.materials import split_pages
from research_agent.research_project import write_input
from research_agent.trace import now_iso

CASE = ROOT / 'datasets/project/auto-research-amem-v1/case.json'
SOURCE = Path('D:/paper/researchagent-sources/AgenticMemory/0c8039f28fdcc08189a23c07a3437d9d2482f9c2')
PAPER = Path('D:/paper/researchagent-sources/A-MEM')
RUNTIME = ROOT / 'experiments/amem-runtime-v1'


def frozen_case_files(case):
    """Select host-owned scoring labels and the public protocol by frozen hash."""
    if case.get("audit_dataset") != "locomo":
        raise ValueError("A-MEM labels require the LoCoMo host scoring and isolation contract")
    files = {}
    for kind, default in [('protocol', 'PROTOCOL.md'), ('labels', 'development-labels.json')]:
        name = case.get(kind + '_source', (CASE.parent / default).relative_to(ROOT).as_posix())
        # CodingTool.private_paths denies the entire datasets tree for LoCoMo.
        if kind == "labels" and (not isinstance(name, str) or not name.startswith("datasets/")):
            raise ValueError("A-MEM labels_source must stay under the private datasets directory")
        raw = regular_file(ROOT, name, limit=16 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != case[kind + '_sha256']:
            raise ValueError('Frozen A-MEM ' + kind + ' changed')
        files[kind] = (name, raw)
    return files


def prepare(app, job, workspace, case):
    selected = frozen_case_files(case)
    split = case.get('split', 'development')
    if split not in ('development', 'holdout'):
        raise ValueError('A-MEM split must be development or holdout')
    for name in ('tasks', 'corpus'):
        rows = json.loads(regular_file(workspace, 'data/locomo/' + name + '.json', limit=16 * 1024 * 1024))
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, dict) or row.get('split') != split for row in rows)):
            raise ValueError('A-MEM split does not match frozen ' + name)
    assets = read(CASE.parent / 'assets.json')
    if sha(CASE.parent / 'assets.json') != case['assets_sha256']:
        raise ValueError('A-MEM frozen assets manifest changed')
    for name, digest in assets['upstream_files'].items():
        if sha(SOURCE / name) != digest:
            raise ValueError('Pinned A-MEM source changed: ' + name)
        write_input(workspace, 'upstream/' + name, (SOURCE / name).read_bytes())
    if sha(PAPER / '2502.12110.pdf') != assets['paper_sha256']:
        raise ValueError('Pinned A-MEM PDF changed')
    parsed = read(PAPER / 'paper-pages.json')
    if sha(PAPER / 'paper-pages.json') != assets['paper_pages_sha256']:
        raise ValueError('Pinned A-MEM extracted text changed')
    for page, text in parsed['pages']:
        write_input(workspace, f'paper/page-{page:02d}.txt', text.encode('utf-8'))
    record = {'kind': 'paper', 'title': parsed['title'], 'url': parsed['source_url'],
              'canonical_id': 'arxiv:2502.12110', 'metadata': {'page_count': parsed['page_count'],
              'pdf_sha256': assets['paper_sha256'], 'extraction': 'pypdf text layer; figures/formulas require original PDF'},
              'chunks': split_pages(parsed['pages']), 'warnings': parsed['warnings'], 'boundary': parsed['boundary'],
              'data': (PAPER / '2502.12110.pdf').read_bytes(), 'filename': '2502.12110.pdf'}
    material, _ = app.library.save(job['space_id'], record, job_id=job['id'], topic='A-MEM baseline', download=True)
    private = app.store.path.resolve().parent / 'auto-research' / job['id']
    private.mkdir(parents=True, exist_ok=True)
    write_input(private, 'scoring-labels.json', selected['labels'][1])
    # This host-prepared environment contains only verified wheels. Large existing
    # dependencies are read through a plain .pth path; no generated interpreter runs here.
    if not RUNTIME.is_dir() or (workspace / '.venv').exists():
        raise ValueError('Prepared runtime missing or project runtime already exists')
    if (RUNTIME / 'Lib/site-packages/research-base.pth').read_text(encoding='utf-8').strip() != str(ROOT / '.venv-v3/Lib/site-packages'):
        raise ValueError('Unexpected shared dependency path')
    runtime_names = {p.relative_to(RUNTIME).as_posix() for p in RUNTIME.rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if runtime_names != set(assets['runtime_files']):
        raise ValueError('Prepared runtime file inventory changed')
    for name, digest in assets['runtime_files'].items():
        if sha(RUNTIME / name) != digest:
            raise ValueError('Prepared runtime changed: ' + name)
    shutil.copytree(RUNTIME, workspace / '.venv', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    write(private / 'environment.json', {'python_sha256': sha(workspace / '.venv/Scripts/python.exe'),
          'packages': assets['packages'], 'provenance': 'Verified wheel-only isolated runtime with shared read-only base dependencies'})
    write_input(workspace, 'PROTOCOL.md', selected['protocol'][1])
    write(private / 'assets.json', assets)
    return {'upstream_revision': assets['upstream_revision'], 'paper_sha256': assets['paper_sha256'],
            'material_id': material['id'], 'runtime_files': len(assets['runtime_files']),
            'labels': 'private host only; never staged into experiment workspace', 'split': split}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='sudocode-luna')
    parser.add_argument('--case-file', type=Path, default=CASE, help='Explicit versioned continuation protocol')
    parser.add_argument('--initial-source', type=Path, help='Saved code snapshot; previous scores are not imported')
    args = parser.parse_args()
    out = args.output.resolve()
    selected_case = args.case_file.resolve()
    data = read(selected_case)
    selected = frozen_case_files(data['campaign'])
    out.mkdir(parents=True, exist_ok=False)
    write(out / 'cases.json', data)
    paths = [*ROOT.glob('research_agent/*.py'), *ROOT.glob('evals/*.py'), *CASE.parent.glob('*')]
    paths += [selected_case, *(ROOT / name for name, _ in selected.values())]
    if args.initial_source:
        paths.append(args.initial_source.resolve())
    paths = list(dict.fromkeys(p for p in paths if p.is_file()))
    write(out / 'condition.json', {'started_at': now_iso(), 'model_id': args.model,
          'source_sha256': {p.relative_to(ROOT).as_posix(): sha(p) for p in paths},
          'sampling': 'Host provider defaults; upstream per-call temperatures are not forwarded by stdio v1.',
          'scientific_effect_verified': False})
    with zipfile.ZipFile(out / 'evaluated-source-and-cases.zip', 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(ROOT).as_posix())
    passed = campaign(out, data, args.model, initial_source=args.initial_source, prepare_workspace=prepare)
    print(json.dumps({'passed': passed, 'output': str(out)}), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
