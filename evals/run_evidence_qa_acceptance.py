"""Paired LOCAL_QA acceptance through Workbench.execute; never opens the user DB.

Freeze sources, labels, code and budgets before running. Each case/arm receives a
SQLite backup of the same source-only seed so reports and memories cannot leak.
Recorded failures count; resume skips terminal attempts and never replays a
request whose response may have been lost. Keywords are only review prefilters.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_agent.library import Library
from research_agent.models import load_dotenv, model_from_env
from research_agent.reports import render_report
from research_agent.retrieval import Retriever, model_identity
from research_agent.strategies import BASELINE, digest, encoded
from research_agent.trace import now_iso, redact, is_sensitive_key, _redact_text
from research_agent.usage import summarize_usage
from research_agent.workbench import Workbench
from research_agent.workbench_store import WorkbenchStore

PANEL = ROOT / 'datasets/evidence_qa/evidence_qa_cases_20260927.json'
CORPUS = ROOT / 'datasets/retrieval/retrieval_corpus_v3.json'
OFFICIAL = ROOT / 'evals/reports/evidence-qa-20260927/acceptance-r1'
ARMS = {'baseline': {**BASELINE, 'evidence_rerank': False},
        'candidate': {**BASELINE, 'reading_guide': True, 'evidence_rerank': True}}
BUDGET = {'research_effort': 'quick', 'tool_calls': 8, 'rounds': 12,
          'context_tokens': 16384, 'max_model_attempts': 32,
          'max_case_seconds': 600, 'max_reported_tokens': 300000}
SCOPE = '仅使用当前研究区的本地资料，不要检索历史对话或记忆，不联网。'


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest():
    # V4 experiment workers are not run on LOCAL_QA. Preserve their hashes in a
    # separate repository snapshot, so parallel experiment development does not
    # mix QA conditions or force us to discard paid results.
    modules = [p for p in ROOT.glob('research_agent/*.py') if not p.name.startswith('experiment') and p.name != 'coding_agent.py']
    paths = [*modules, Path(__file__).resolve(), PANEL, CORPUS]
    return {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p) for p in sorted(paths)}


def repository_manifest():
    return {p.name: sha(p) for p in sorted(ROOT.glob('research_agent/*.py'))}


def sanitize_full(value, secret=''):
    """Same credential boundary as traces without their 12k display truncation."""
    if isinstance(value, dict):
        return {k: '[REDACTED]' if is_sensitive_key(str(k)) else sanitize_full(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_full(v, secret) for v in value]
    if isinstance(value, str):
        text = value.replace(secret, '[REDACTED]') if secret else value
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return _redact_text(text)
        if isinstance(parsed, (dict, list)):
            cleaned = sanitize_full(parsed, secret)
            return text if parsed == cleaned else json.dumps(cleaned, ensure_ascii=False)
        return _redact_text(text)
    return value


def model_condition(model):
    return {'name': model.name, 'temperature': getattr(model, 'temperature', None),
            'timeout_seconds': getattr(model, 'timeout', None),
            'endpoint_hash': hashlib.sha256(getattr(model, 'base_url', '').encode()).hexdigest()}


def validate_panel(panel, docs):
    by_key = {d['key']: d for d in docs}
    counts = {}
    ids = set()
    for case in panel['cases']:
        if case['id'] in ids:
            raise ValueError('Duplicate case id')
        ids.add(case['id'])
        counts[case['category']] = counts.get(case['category'], 0) + 1
        refs = [e for f in case['facts'] for e in f['evidence']]
        refs += case.get('absence_basis', {}).get('related_originals', [])
        if not refs or bool(case['facts']) == case['unanswerable']:
            raise ValueError('Every case needs an explicit evidence/absence boundary')
        for e in refs:
            d = by_key[e['document']]
            c = d['chunks'][e['ordinal']]
            if e['quote'] != c['content'] or e['url'] != d['url'] or e['page'] != c.get('page'):
                raise ValueError('Original quote, source, or page changed')
            if e['sha256'] != hashlib.sha256(e['quote'].encode()).hexdigest():
                raise ValueError('Original quote digest mismatch')
    if counts != {'ordinary': 4, 'cross_document': 4, 'false_premise': 4, 'unanswerable': 4}:
        raise ValueError('The frozen panel must contain 16 balanced cases')
    return counts


def freeze(out, model_id, case_ids=None, *, model_settings=None, retrieval_settings=None):
    panel, docs = read(PANEL), read(CORPUS)
    validate_panel(panel, docs)
    if panel['source_corpus_sha256'] != sha(CORPUS):
        raise ValueError('Panel was authored against a different corpus')
    selected = [c['id'] for c in panel['cases'] if not case_ids or c['id'] in case_ids]
    if case_ids and set(case_ids) != set(selected):
        raise ValueError('Unknown case id')
    if out.resolve() == OFFICIAL.resolve() and case_ids:
        raise ValueError('Official acceptance requires all 16 cases; use another output for debug')
    condition = {'schema': 'evidence-qa-condition/v1', 'model_id': model_id,
                 'model_settings': model_settings, 'retrieval_settings': retrieval_settings,
                 'panel_hash': digest(panel), 'source_hash': digest(docs),
                 'budget': BUDGET, 'arms': ARMS, 'question_prefix': SCOPE,
                 'case_ids': selected, 'source_manifest': manifest(),
                 'formal': out.resolve() == OFFICIAL.resolve(),
                 'scoring': 'manual fact/citation review; keyword checks are only prefilters',
                 'path': 'Workbench.execute / LOCAL_QA; explicit local intent, routing excluded',
                 'failure_policy': 'first attempt retained; resume never repeats an incomplete request'}
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'condition.json'
    if path.exists() and read(path) != condition:
        raise ValueError('Frozen condition changed; keep existing attempts and use a new debug output')
    write(path, condition)
    write(out / 'panel.json', panel)
    write(out / 'sources.json', docs)
    if not (out / 'repository-start-manifest.json').exists():
        write(out / 'repository-start-manifest.json', repository_manifest())
    return condition


def assert_frozen(out):
    condition = read(out / 'condition.json')
    if condition['source_manifest'] != manifest():
        raise ValueError('Product code/dataset changed after freeze; refusing mixed-condition results')
    if digest(read(out / 'panel.json')) != condition['panel_hash'] or digest(read(out / 'sources.json')) != condition['source_hash']:
        raise ValueError('Frozen panel/source copy changed')
    return condition


def seed(out):
    seed_path = out / 'seed.sqlite'
    if (out / 'seed.json').exists():
        return read(out / 'seed.json')
    if seed_path.exists():
        raise ValueError('Incomplete seed; preserve it and use a new debug output')
    store = WorkbenchStore(seed_path)
    sid = store.save_space({'name': 'Evidence QA frozen public sources', 'download_root': r'D:\paper'})['id']
    lib = Library(store)
    chunk_map = {}
    for d in read(out / 'sources.json'):
        item, _ = lib.save(sid, {'kind': 'paper' if d['key'] in ('docling', 'react') else 'document',
            'title': d['title'], 'url': d['url'], 'canonical_id': d['key'], 'metadata': d['metadata'],
            'data': None, 'warnings': [], 'boundary': 'Frozen original public source',
            'chunks': [dict(c, text=c['content']) for c in d['chunks']]})
        for c in lib.chunks(sid, item['id']):
            chunk_map[str(c['id'])] = [d['key'], c['ordinal']]
    retrieval = Retriever(store, lib)
    retrieval.sync(sid)
    if retrieval.status(sid)['mode'] != 'hybrid' or retrieval.status(sid)['coverage'] != 1:
        raise RuntimeError('Public seed hybrid index is not fully available; no model calls started')
    info = {'space_id': sid, 'chunk_map': chunk_map, 'retrieval_status': retrieval.status(sid),
            'source_hash': digest(read(out / 'sources.json'))}
    write(out / 'seed.json', info)
    return info


def pin_arm(store, job, arm, cfg):
    """Trusted evaluator-only snapshot in the isolated DB; no product activation."""
    vid = 'acceptance-' + arm
    snapshot = {'id': vid, 'config': cfg, 'config_hash': digest(cfg),
                'diagnosis': 'Frozen paired evaluation', 'trigger': 'isolated_acceptance'}
    with closing(store._connect()) as db, db:
        db.execute('INSERT INTO harness_versions VALUES(?,?,?,?,?,?,?,?,?)',
                   (vid, job['space_id'], None, arm, encoded(cfg), digest(cfg), 'Paired acceptance', None, now_iso()))
        db.execute('INSERT INTO harness_runs VALUES(?,?,?,?,?,?,?,?)',
                   (job['id'], job['space_id'], vid, job['model_name'], snapshot['trigger'], encoded(snapshot), None, now_iso()))
    return snapshot


def instrument(model, folder, budget):
    """Log actual provider attempts without credentials; retries stay separate."""
    raw = model._complete
    started = time.monotonic()
    counter = 0
    request_dir = folder / 'requests'
    request_dir.mkdir(exist_ok=True)

    def safe(value):
        return sanitize_full(value, getattr(model, 'api_key', ''))

    def recorded(messages, tools):
        nonlocal counter
        elapsed = time.monotonic() - started
        known_tokens = sum(r.get('total_tokens') or 0 for r in model.usage_records)
        if counter >= budget['max_model_attempts'] or elapsed >= budget['max_case_seconds'] or known_tokens >= budget['max_reported_tokens']:
            raise RuntimeError('Frozen acceptance request/time/reported-token budget exhausted')
        counter += 1
        path = request_dir / f'{counter:03d}.json'
        item = {'attempt_id': counter, 'started_at': now_iso(), 'purpose': model.usage_purpose,
                'model': model.name, 'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)}
        item['request_hash'] = digest({'messages': item['messages'], 'tools': item['tools']})
        write(path, safe(item))
        before = time.perf_counter()
        model._active_timeout = min(getattr(model, '_active_timeout', model.timeout), budget['max_case_seconds'] - elapsed)
        try:
            result = raw(messages, tools)
            item['response'] = dataclasses.asdict(result)
            return result
        except Exception as exc:
            item['error'] = type(exc).__name__ + ': ' + str(redact(str(exc)))
            raise
        finally:
            item['seconds'] = time.perf_counter() - before
            item['usage'] = model.last_usage
            item['finished_at'] = now_iso()
            write(path, safe(item))
    model._complete = recorded
    return model


def prefilter(case, answer):
    return {f['id']: all(re.search(p, answer, re.I) for p in f['prefilter_patterns']) for f in case['facts']}


def score(case, arm, job, run, events, usage, chunk_map, seconds):
    answer = job.get('summary') or (run or {}).get('answer', '')
    cited = set(re.findall(r'\[E(\d+)\]', answer))
    originals = [e for e in (run or {}).get('evidence', []) if e['kind'].startswith('local:')]
    def key(e):
        return tuple(chunk_map.get(str(e['provenance'].get('chunk_id')), []))
    read_keys = {key(e) for e in originals} - {()}
    cited_keys = {key(e) for e in originals if e['evidence_id'][1:] in cited} - {()}
    gold = {(e['document'], e['ordinal']) for f in case['facts'] for e in f['evidence']}
    gold_text = {(e['document'], e['ordinal']): e['quote'] for f in case['facts'] for e in f['evidence']}
    full_reads = []
    for source_key, original in gold_text.items():
        covered = set()
        for evidence in originals:
            if key(evidence) != source_key:
                continue
            start = original.find(evidence['content'])
            if start >= 0:
                covered.update(range(start, start + len(evidence['content'])))
        full_reads.append(len(covered) == len(original))
    claims = (run or {}).get('claims', [])
    expected = prefilter(case, answer)
    return {'id': case['id'], 'arm': arm, 'category': case['category'],
            'unanswerable': case['unanswerable'], 'job_id': job.get('id'),
            'status': job.get('status'), 'error': job.get('error'),
            'termination': (run or {}).get('termination', job.get('status')),
            'product_completed': job.get('status') == 'completed',
            'prefilter': expected, 'all_prefilter_facts_matched': all(expected.values()) if expected else None,
            'harness_claims_supported': all(c['status'] == 'SUPPORTED' for c in claims) if claims else None,
            'claim_count': len(claims), 'seconds': seconds, 'usage': summarize_usage(usage),
            'tool_calls': (run or {}).get('tool_calls'),
            'original_read_calls': sum(e['kind'] == 'tool_call_requested' and e['payload'].get('name') == 'read_evidence' for e in events),
            'gold_read_coverage': len(gold & read_keys) / len(gold) if gold else None,
            'gold_citation_coverage': len(gold & cited_keys) / len(gold) if gold else None,
            'gold_fully_read_passage_rate': statistics.mean(full_reads) if full_reads else None,
            'read_originals': sorted(read_keys), 'cited_originals': sorted(cited_keys),
            'answer_sha256': hashlib.sha256(answer.encode()).hexdigest(),
            'manual_review_required': True}


def export_attempt(folder, case, arm, store, info, job, seconds, usage=None):
    run = store.get(job['run_id']) if job.get('run_id') else None
    with closing(store._connect()) as db:
        events = [dict(r) for r in db.execute('SELECT * FROM conversation_events WHERE job_id=? ORDER BY id', (job['id'],))]
    for event in events:
        event['payload'] = json.loads(event['payload'])
    requests = [read(p) for p in sorted((folder / 'requests').glob('*.json'))]
    if usage is None:
        usage = [{**r.get('usage', {}), 'model': r['model'], 'purpose': r['purpose'],
                  'latency_ms': r.get('seconds', 0) * 1000, 'error': r.get('error')} for r in requests]
    row = score(case, arm, job, run, events, usage, info['chunk_map'], seconds)
    row['provider_attempts'] = len(requests)
    row['incomplete_provider_responses'] = sum('finished_at' not in r for r in requests)
    write(folder / 'job.json', job)
    write(folder / 'run.json', run)
    write(folder / 'events.json', events)
    write(folder / 'evidence-rerank.json', [e for e in events if e['kind'] == 'harness_retrieval'])
    write(folder / 'usage.json', usage)
    (folder / 'answer.md').write_text(job.get('summary') or job.get('error') or '', encoding='utf-8')
    review = {'id': case['id'], 'arm': arm, 'answer_sha256': row['answer_sha256'], 'reviewer': '', 'reviewed_at': '',
              'facts': [{'id': f['id'], 'correct': None, 'supported': None, 'citation_sufficient': None,
                         'actual_citations': [], 'reason': ''} for f in case['facts']],
              'refused': None, 'refusal_correct': None if case['unanswerable'] else False,
              'unsupported_additions': None, 'task_pass': None, 'notes': ''}
    if not (folder / 'review.json').exists():
        write(folder / 'review.json', review)
    write(folder / 'score.json', row)
    return row


def run_one(out, condition, info, case, arm):
    folder = out / 'cases' / case['id'] / arm
    if (folder / 'score.json').exists():
        return read(folder / 'score.json')
    if folder.exists():
        # An unknown in-flight outcome may already have incurred cost: never replay it.
        if (folder / 'public.sqlite').exists():
            store = WorkbenchStore(folder / 'public.sqlite')
            jobs = store.jobs(info['space_id'])
            if len(jobs) == 1:
                job = jobs[0]
                if job['status'] in {'queued', 'running'}:
                    store.interrupt_pending()
                    job = store.job(info['space_id'], job['id'])
                return export_attempt(folder, case, arm, store, info, job, None)
        row = {'id': case['id'], 'arm': arm, 'category': case['category'], 'unanswerable': case['unanswerable'],
               'status': 'interrupted', 'error': 'Reserved attempt lacks terminal record; not repeated',
               'seconds': None, 'product_completed': False, 'manual_review_required': True,
               'usage': summarize_usage([]), 'answer_sha256': None}
        write(folder / 'score.json', row)
        return row
    folder.mkdir(parents=True)
    write(folder / 'started.json', {'condition_hash': digest(condition), 'at': now_iso()})
    with closing(sqlite3.connect(out / 'seed.sqlite')) as source, closing(sqlite3.connect(folder / 'public.sqlite')) as target:
        source.backup(target)
    store = WorkbenchStore(folder / 'public.sqlite')
    sid = info['space_id']
    models = []
    def model_factory(*, model_id='default'):
        model = model_from_env(condition['model_id'])
        if condition.get('model_settings') and model_condition(model) != condition['model_settings']:
            raise ValueError('Actual model settings changed after freeze')
        model = instrument(model, folder, condition['budget'])
        models.append(model)
        return model
    def no_external_agent(*args, **kwargs):
        raise AssertionError('Acceptance must execute LOCAL_QA')
    work = Workbench(store, model_factory, no_external_agent, folder / 'traces', start_worker=False)
    # The SQLite backup contains vector status, but Chroma files are not copied.
    # Rebuild each isolated vector index from immutable text; in-process encoder
    # caching reuses embeddings without sharing any reports or memory content.
    with closing(store._connect()) as db, db:
        db.execute("UPDATE retrieval_index_state SET status='pending'")
    work.retrieval.sync(sid)
    retrieval_status = work.retrieval.status(sid)
    write(folder / 'retrieval-status.json', retrieval_status)
    if retrieval_status['mode'] != 'hybrid' or retrieval_status['coverage'] != 1:
        raise RuntimeError('Case hybrid index unavailable; refusing silently degraded comparison')
    chat = store.create_conversation(sid, case['id'] + ' / ' + arm)['id']
    question = SCOPE + '\n' + case['query']
    store.message(sid, chat, 'user', question, model_id=condition['model_id'])
    job = store.enqueue(sid, chat, question, case['query'], [], research_effort=condition['budget']['research_effort'],
                        model_id=condition['model_id'], kind='LOCAL_QA')
    write(folder / 'strategy.json', pin_arm(store, job, arm, condition['arms'][arm]))
    started = time.perf_counter()
    claimed = store.claim_next()
    if not claimed or claimed['id'] != job['id']:
        raise RuntimeError('Isolated job claim failed')
    work.execute(claimed)
    seconds = time.perf_counter() - started
    job = store.job(sid, job['id'])
    usage = [r for model in models for r in model.usage_records]
    return export_attempt(folder, case, arm, store, info, job, seconds, usage)


def reviewed_result(row, review, case):
    if not review or review.get('answer_sha256') != row.get('answer_sha256') or not review.get('reviewer') or not review.get('reviewed_at'):
        return None
    facts = review.get('facts', [])
    if len(facts) != len(case['facts']) or {f.get('id') for f in facts} != {f['id'] for f in case['facts']}:
        return None
    if any(type(f.get(k)) is not bool for f in facts for k in ('correct', 'supported', 'citation_sufficient')):
        return None
    if any(type(review.get(k)) is not bool for k in ('unsupported_additions', 'task_pass', 'refused')):
        return None
    if case['unanswerable'] and type(review.get('refusal_correct')) is not bool:
        return None
    derived = row.get('product_completed', False) and not review['unsupported_additions'] and (review['refusal_correct'] and review['refused'] if case['unanswerable'] else all(
        all(f[k] for k in ('correct', 'supported', 'citation_sufficient')) for f in facts))
    if review['task_pass'] != derived:
        raise ValueError('Manual task_pass contradicts per-fact review')
    return derived


def report(out):
    panel, condition = read(out / 'panel.json'), read(out / 'condition.json')
    cases = {c['id']: c for c in panel['cases']}
    rows = []
    for cid in condition['case_ids']:
        for arm in ARMS:
            folder = out / 'cases' / cid / arm
            if not (folder / 'score.json').exists():
                continue
            row = read(folder / 'score.json')
            review = read(folder / 'review.json') if (folder / 'review.json').exists() else None
            row['reviewed_task_pass'] = reviewed_result(row, review, cases[cid])
            row['reviewed_refused'] = review['refused'] if row['reviewed_task_pass'] is not None else None
            row['reviewed_facts'] = review['facts'] if row['reviewed_task_pass'] is not None else []
            row['artifact'] = str(folder.relative_to(out)).replace('\\', '/')
            rows.append(row)
    metrics = {}
    for arm in ARMS:
        subset = [r for r in rows if r['arm'] == arm]
        reviewed = [r for r in subset if r['reviewed_task_pass'] is not None]
        times = sorted(r['seconds'] for r in subset if r.get('seconds') is not None)
        metrics[arm] = {'attempts': len(subset), 'reviewed': len(reviewed),
            'reviewed_passed': sum(r['reviewed_task_pass'] for r in reviewed),
            'reviewed_task_pass_rate': statistics.mean(r['reviewed_task_pass'] for r in reviewed) if reviewed else None,
            'product_completed': sum(r.get('product_completed', False) for r in subset),
            'seconds_mean': statistics.mean(times) if times else None,
            'seconds_p95': times[min(len(times)-1, int(.95 * len(times)))] if times else None,
            'reported_tokens': sum(r['usage']['total_tokens'] or 0 for r in subset),
            'requests': sum(r['usage']['requests'] for r in subset),
            'provider_attempts': sum(r.get('provider_attempts', 0) for r in subset),
            'usage_complete_runs': sum(r['usage'].get('total_tokens_coverage') == 1 for r in subset)}
        for key in ('gold_read_coverage', 'gold_citation_coverage', 'gold_fully_read_passage_rate'):
            values = [r[key] for r in subset if r.get(key) is not None]
            metrics[arm][key] = statistics.mean(values) if values else None
        absent = [r for r in reviewed if r['unanswerable']]
        answerable = [r for r in reviewed if not r['unanswerable']]
        metrics[arm]['correct_refusal_rate'] = statistics.mean(r['reviewed_task_pass'] for r in absent) if absent else None
        metrics[arm]['answerable_task_pass_rate'] = statistics.mean(r['reviewed_task_pass'] for r in answerable) if answerable else None
        metrics[arm]['false_refusal_rate'] = statistics.mean(r['reviewed_refused'] for r in answerable) if answerable else None
        facts = [f for r in reviewed for f in r['reviewed_facts']]
        metrics[arm]['reviewed_fact_accuracy'] = statistics.mean(f['correct'] for f in facts) if facts else None
        metrics[arm]['reviewed_citation_sufficiency'] = statistics.mean(f['supported'] and f['citation_sufficient'] for f in facts) if facts else None
    write(out / 'results.json', rows)
    write(out / 'metrics.json', metrics)
    write(out / 'failures.json', [r for r in rows if not r.get('product_completed') or r['reviewed_task_pass'] is False])
    md = '# 原文重排完整问答验收\n\n相同模型、固定 16 题、相同 quick 预算，调用实际 Workbench.execute / LOCAL_QA。每题每臂独立数据库；对照顺序交错。路由已指定为本地问答，路由准确率不在本次范围。\n\n'
    md += '这是六个既有公开来源的小样本自编面板，不是独立来源泛化结论。关键词只供预筛，任务通过率使用 Codex 逐事实阅读回答与原文的复核；这不是独立人类评分或额外 judge API。未复核不能计为通过。\n\n'
    md += '|策略|尝试|已复核|复核通过|平均秒|已报告 token|\n|---|---:|---:|---:|---:|---:|\n'
    for arm, m in metrics.items():
        md += f"|{arm}|{m['attempts']}|{m['reviewed']}|{m['reviewed_passed']}|{round(m['seconds_mean'], 1) if m['seconds_mean'] is not None else '未知'}|{m['reported_tokens']}|\n"
    md += '\n总耗时包含模型、工具、核验、保存报告与记忆后处理。token 缺报不计零成本；预算在下一请求前检查，单次最后响应可能越过已报告 token 门槛。\n\n'
    md += 'gold_read_coverage 表示已打开固定 gold 片段，不代表读完片段或全文；gold_fully_read_passage_rate 要求 read_evidence 的实际可见文字覆盖整段。引用充分性仍须逐事实看内容。\n\n'
    for r in rows:
        md += f"- {r['id']} / {r['arm']}：产品 {r['status']}，复核 {r['reviewed_task_pass']}。[回答]({r['artifact']}/answer.md) · [原文与结论]({r['artifact']}/run.json) · [复核]({r['artifact']}/review.json) · [请求]({r['artifact']}/requests/)\n"
    (out / 'report.md').write_text(md, encoding='utf-8')
    (out / 'report.html').write_text(render_report(md, '原文重排完整问答验收'), encoding='utf-8')
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('freeze', 'run', 'report'), required=True)
    parser.add_argument('--output', type=Path, default=OFFICIAL)
    parser.add_argument('--model', default='sudocode-luna')
    parser.add_argument('--case', action='append', dest='cases')
    args = parser.parse_args()
    out = args.output.resolve()
    if args.stage == 'freeze':
        load_dotenv(ROOT / '.env')
        model = model_from_env(args.model)
        retrieval_settings = {'mode': 'hybrid', 'device': os.getenv('BGE_DEVICE', 'cpu'),
            'identity': model_identity(Path(os.getenv('BGE_MODEL_PATH', str(ROOT / 'data/models/bge-m3'))))}
        condition = freeze(out, args.model, args.cases, model_settings=model_condition(model),
                           retrieval_settings=retrieval_settings)
        print(json.dumps({'output': str(out), 'condition_hash': digest(condition), 'model': model.name,
                          'cases': len(condition['case_ids']), 'formal': condition['formal']}, ensure_ascii=False), flush=True)
        return
    if args.stage == 'report':
        print(json.dumps(report(out), ensure_ascii=False), flush=True)
        return
    condition = assert_frozen(out)
    if args.cases and args.cases != condition['case_ids']:
        raise ValueError('Run cases are selected by frozen condition; use --case only with freeze')
    load_dotenv(ROOT / '.env')
    os.environ.setdefault('BGE_MODEL_PATH', str(ROOT / 'data/models/bge-m3'))
    os.environ['RETRIEVAL_MODE'] = 'hybrid'
    actual_identity = model_identity(Path(os.environ['BGE_MODEL_PATH']))
    if condition.get('retrieval_settings') and condition['retrieval_settings'] != {
            'mode': 'hybrid', 'device': os.getenv('BGE_DEVICE', 'cpu'), 'identity': actual_identity}:
        raise ValueError('Encoder or retrieval configuration changed after freeze')
    info = seed(out)
    cases = {c['id']: c for c in read(out / 'panel.json')['cases']}
    for index, cid in enumerate(condition['case_ids']):
        for arm in (('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')):
            assert_frozen(out)
            row = run_one(out, condition, info, cases[cid], arm)
            print(json.dumps({k: row.get(k) for k in ('id', 'arm', 'status', 'seconds', 'error')}, ensure_ascii=False), flush=True)
            report(out)
    write(out / 'end-manifest.json', {'unchanged': condition['source_manifest'] == manifest(), 'files': manifest()})
    write(out / 'repository-end-manifest.json', repository_manifest())


if __name__ == '__main__':
    main()
