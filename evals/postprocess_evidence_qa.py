"""Auditable scoring correction; leaves frozen execution and first scores intact.

EvidenceCatalog stores full originals even when read_evidence returns a bounded
window. Count full reading from actual answer-model request windows, never from
the stored full evidence. No network/model calls occur in this script.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path
from urllib.parse import quote, urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.run_evidence_qa_acceptance import read, write, reviewed_result, OFFICIAL
from research_agent.reports import render_report


def digest_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def windows(requests):
    """Only windows actually submitted on successful answer/repair requests."""
    result = []
    for request in requests:
        if request.get('purpose') not in {'answer', 'answer_repair'} or 'response' not in request:
            continue
        for message in request.get('messages', []):
            if message.get('role') != 'tool':
                continue
            try:
                payload = json.loads(message.get('content', ''))
            except (TypeError, ValueError):
                continue
            payload = payload.get('UNTRUSTED_TOOL_DATA', payload) if isinstance(payload, dict) else {}
            if payload.get('kind') != 'read_evidence' or not payload.get('ok'):
                continue
            for part in [payload, *payload.get('neighbors', [])]:
                if part.get('corpus') == 'documents' and isinstance(part.get('content'), str):
                    result.append({'chunk_id': part.get('chunk_id'), 'evidence_id': part.get('evidence_id'),
                                   'content': part['content'], 'request_attempt_id': request.get('attempt_id'),
                                   'start_offset': part.get('start_offset'), 'total_chars': part.get('total_chars'),
                                   'content_chars': part.get('content_chars'), 'next_offset': part.get('next_offset'),
                                   'context_excerpted': bool(part.get('context_excerpted'))})
    return result


def reading_audit(case, requests, chunk_map):
    originals = {(e['document'], e['ordinal']): e['quote'] for f in case['facts'] for e in f['evidence']}
    if not originals:
        return {'gold_chunk_seen_rate': None, 'gold_fully_read_passage_rate': None, 'gold_character_coverage': None, 'passages': []}
    actual = windows(requests)
    passages = []
    for key, original in originals.items():
        covered, observed, unmapped = set(), [], []
        for window in actual:
            if tuple(chunk_map.get(str(window['chunk_id']), [])) != key:
                continue
            offset = window['start_offset']
            declared = type(offset) is int and 0 <= offset <= len(original)
            if window['total_chars'] is not None and window['total_chars'] != len(original):
                unmapped.append({'request_attempt_id': window['request_attempt_id'], 'reason': 'original_length_mismatch'})
                continue
            if declared and not window['context_excerpted']:
                text = window['content']
                if original[offset:offset + len(text)] != text:
                    unmapped.append({'request_attempt_id': window['request_attempt_id'], 'reason': 'offset_content_mismatch'})
                    continue
                fragments = [(offset, text)]
            else:
                # A compaction excerpt still comes from the original read window,
                # not an arbitrary equal string elsewhere in the full passage.
                left, right = (offset, min(len(original), offset + 1800)) if declared else (0, len(original))
                if declared and type(window['content_chars']) is int and 0 <= window['content_chars'] <= 1800:
                    right = min(right, offset + window['content_chars'])
                fragments = []
                for text in re.split(r'\n\[\.\.\. omitted \.\.\.\]\n', window['content']):
                    if not text:
                        continue
                    first = original.find(text, left, right)
                    second = original.find(text, first + 1, right) if first >= 0 else -1
                    if first < 0 or second >= 0:
                        unmapped.append({'request_attempt_id': window['request_attempt_id'],
                                         'reason': 'unmatched_excerpt' if first < 0 else 'ambiguous_repeated_excerpt',
                                         'excerpt_chars': len(text), 'search_start': left, 'search_end': right})
                        continue
                    fragments.append((first, text))
            # Context compaction may preserve disjoint verbatim excerpts. Omitted
            # markers are not source characters and must not count as reading.
            for start, text in fragments:
                covered.update(range(start, start + len(text)))
                observed.append({'request_attempt_id': window['request_attempt_id'],
                                 'evidence_id': window['evidence_id'], 'start': start, 'end': start + len(text)})
        passages.append({'document': key[0], 'ordinal': key[1], 'original_chars': len(original),
                         'seen_chars': len(covered), 'fully_read': len(covered) == len(original),
                         'windows': [dict(x) for x in sorted({tuple(sorted(x.items())) for x in observed})],
                         'unmapped_windows': unmapped})
    return {'gold_chunk_seen_rate': statistics.mean(p['seen_chars'] > 0 for p in passages),
            'gold_fully_read_passage_rate': statistics.mean(p['fully_read'] for p in passages),
            'gold_character_coverage': sum(p['seen_chars'] for p in passages) / sum(p['original_chars'] for p in passages),
            'passages': passages}


def summarize(rows):
    result = {}
    for arm in ('baseline', 'candidate'):
        selected = [r for r in rows if r['arm'] == arm]
        reviewed = [r for r in selected if r['reviewed_task_pass'] is not None]
        facts = [f for r in reviewed for f in r['review']['facts']]
        values = {'attempts': len(selected), 'reviewed': len(reviewed),
                  'passed': sum(r['reviewed_task_pass'] for r in reviewed),
                  'product_completed': sum(r.get('product_completed', False) for r in selected),
                  'reported_tokens': sum(r['usage'].get('total_tokens') or 0 for r in selected),
                  'provider_attempts': sum(r.get('provider_attempts', 0) for r in selected),
                  'usage_complete_runs': sum(r['usage'].get('total_tokens_coverage') == 1 for r in selected)}
        for field in ('gold_chunk_seen_rate', 'gold_fully_read_passage_rate', 'gold_character_coverage'):
            data = [r['reading'][field] for r in selected if r['reading'][field] is not None]
            values[field] = statistics.mean(data) if data else None
        values['known_gold_chunk_miss_rate'] = 1 - values['gold_chunk_seen_rate'] if values['gold_chunk_seen_rate'] is not None else None
        values['task_pass_rate'] = statistics.mean(r['reviewed_task_pass'] for r in reviewed) if reviewed else None
        values['fact_accuracy'] = statistics.mean(f['correct'] for f in facts) if facts else None
        values['citation_sufficiency'] = statistics.mean(f['supported'] and f['citation_sufficient'] for f in facts) if facts else None
        answerable = [r for r in reviewed if not r['unanswerable']]
        absent = [r for r in reviewed if r['unanswerable']]
        values['false_refusal_rate'] = statistics.mean(r['review']['refused'] for r in answerable) if answerable else None
        values['correct_refusal_rate'] = statistics.mean(r['review']['refusal_correct'] for r in absent) if absent else None
        values['answerable_task_pass_rate'] = statistics.mean(r['reviewed_task_pass'] for r in answerable) if answerable else None
        values['unanswerable_task_pass_rate'] = statistics.mean(r['reviewed_task_pass'] for r in absent) if absent else None
        times = sorted(r['seconds'] for r in selected if r.get('seconds') is not None)
        values['seconds_mean'] = statistics.mean(times) if times else None
        values['seconds_p95'] = times[min(len(times)-1, int(.95 * len(times)))] if times else None
        values['categories'] = {category: {'reviewed': sum(r['category'] == category for r in reviewed),
            'passed': sum(r['reviewed_task_pass'] for r in reviewed if r['category'] == category)}
            for category in ('ordinary', 'cross_document', 'false_premise', 'unanswerable')}
        result[arm] = values
    return result


def build(out):
    condition, panel, info = read(out / 'condition.json'), read(out / 'panel.json'), read(out / 'seed.json')
    cases = {c['id']: c for c in panel['cases']}
    rows, inputs = [], {}
    for path in (out / 'condition.json', out / 'panel.json', out / 'seed.json'):
        inputs[path.relative_to(out).as_posix()] = digest_file(path)
    for cid in condition['case_ids']:
        for arm in ('baseline', 'candidate'):
            folder = out / 'cases' / cid / arm
            if not (folder / 'score.json').exists():
                continue
            row = read(folder / 'score.json')
            review = read(folder / 'review.json') if (folder / 'review.json').exists() else None
            requests = [read(p) for p in sorted((folder / 'requests').glob('*.json'))]
            row['reading'] = reading_audit(cases[cid], requests, info['chunk_map'])
            row['reviewed_task_pass'] = reviewed_result(row, review, cases[cid])
            row['review'] = review
            row['artifact'] = folder.relative_to(out).as_posix()
            rows.append(row)
            for path in (folder / 'score.json', folder / 'review.json', *sorted((folder / 'requests').glob('*.json'))):
                if path.exists():
                    inputs[path.relative_to(out).as_posix()] = digest_file(path)
    target = out / 'review-summary'
    metrics = summarize(rows)
    write(target / 'metrics.json', metrics)
    write(target / 'results.json', rows)
    write(target / 'failures.json', [r for r in rows if r['reviewed_task_pass'] is False or not r.get('product_completed')])
    write(target / 'scorer-manifest.json', {'scorer': 'postprocess-evidence-qa/v2', 'source_sha256': digest_file(__file__),
        'review_validation_sha256': digest_file(ROOT / 'evals/run_evidence_qa_acceptance.py'), 'inputs': inputs,
        'erratum': 'The original gold_fully_read_passage_rate used full stored EvidenceCatalog originals. Corrected reading uses actual successful answer/repair request windows only. Original execution, scores, prompts, and failures remain untouched.'})
    md = '# 完整问答验收：逐事实复核与实际阅读窗口\n\n'
    md += f"冻结的 {len(condition['case_ids'])} 题、同一模型和 quick 预算，运行 Workbench LOCAL_QA 全链。每题每臂独立数据库；基线与候选执行顺序交错。\n\n"
    md += '来源边界：' + panel.get('evaluation_scope', '以本次冻结 panel.json 和 sources.json 记载为准。') + '\n\n'
    md += '**评分勘误：** 首版 gold_fully_read_passage_rate 错用了证据库保存的整段原文。本报告改按实际成功回答/修复请求中的 read_evidence 可见窗口计算；原始分数保留，不改变运行或答案。后台重排原文、核验器原文和检索预览不算回答模型实际阅读。\n\n'
    md += '任务质量由 Codex 逐条阅读答案、引用和原文复核，非独立人类评分，非额外 judge API；关键词仅预筛，未复核不计通过。固定 gold 之外的充分引用可在事实复核中认可，但不修改冻结 gold。事实正确率和引用充分性评价用户可见内容中的目标事实（包括明确标注待核验的可见草稿）；完全未交付目标事实计未完成，不能解读为模型已生成了错误事实。端到端通过仍要求产品核验完成。\n\n'
    md += '|策略|已复核/尝试|通过|实际打开 gold 片段|整段读完|平均秒|已报告 token|\n|---|---:|---:|---:|---:|---:|---:|\n'
    for arm, m in metrics.items():
        pct = lambda x: f'{x:.1%}' if x is not None else '未知'
        sec = f"{m['seconds_mean']:.1f}" if m['seconds_mean'] is not None else '未知'
        md += f"|{arm}|{m['reviewed']}/{m['attempts']}|{m['passed']}|{pct(m['gold_chunk_seen_rate'])}|{pct(m['gold_fully_read_passage_rate'])}|{sec}|{m['reported_tokens']}|\n"
    md += '\n耗时包含核验、落盘与记忆后处理；token 缺失不计零。全片段阅读不等于通读文档，读完也不等于事实理解；引用充分性需逐项内容核对。正确拒答率评价拒答内容，端到端任务通过还要求产品完成，因此正确拒答但核验失败仍记任务失败。\n\n'
    for r in rows:
        state = '待复核' if r['reviewed_task_pass'] is None else '通过' if r['reviewed_task_pass'] else '未通过'
        md += f"- {r['id']} / {r['arm']}：{state}，产品 {r['status']}。[回答](../{r['artifact']}/answer.md) · [复核](../{r['artifact']}/review.json) · [轨迹](../{r['artifact']}/events.json) · [原文与引用](../{r['artifact']}/run.json) · [请求用量](../{r['artifact']}/usage.json)\n"
    (target / 'report.md').write_text(md, encoding='utf-8')
    base_url = 'http://127.0.0.1:8000/evidence-qa-evaluation/' + quote(out.name) + '/review-summary/'
    html_md = re.sub(r'\]\((\.\.?/[^)]+)\)', lambda match: '](' + urljoin(base_url, match[1]) + ')', md)
    (target / 'report.html').write_text(render_report(html_md, '完整问答验收复核'), encoding='utf-8')
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OFFICIAL)
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), ensure_ascii=False, indent=2))
