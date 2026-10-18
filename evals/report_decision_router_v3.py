"""Recompute the AUTO confirmation impact and summarize frozen v3 evaluations."""
from __future__ import annotations

import argparse
from collections import Counter
import math
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.build_intent_routing_v2 import read, write, write_atomic, sha


def group_args(study, group):
    four_b = group.endswith('4B')
    config_path = ROOT/'config'/('decision_router_c1_4b.json' if four_b else 'decision_router.json')
    config = read(config_path)
    return SimpleNamespace(group=group, split='test_v3', out=study/('test-'+group),
        model=ROOT/config['model_path'], adapter=ROOT/config['adapter_path'] if group != 'A' and config.get('adapter_path') else None,
        config=config_path if group != 'A' else None, order=config['order'] if group != 'A' else 'forward',
        max_chars=config['max_chars'], encoder=ROOT/'data/models/bge-m3', remote_model='sudocode-luna',
        device='cuda', plan=study/'test-plan.json')


def prepare_plan(study, out):
    from evals.build_intent_routing_v3 import verify_frozen
    from evals.run_decision_router_eval import evaluation_identity, source_manifest, LIMITS
    verify_frozen()
    if out.exists():
        raise FileExistsError(out)
    selection = read(study/'four-b-selection.json')
    if selection['config_sha256'] != sha(ROOT/'config/decision_router_c1_4b.json'):
        raise ValueError('4B config changed after dev selection')
    for candidate in selection['candidates']:
        run = study/('dev-C1-4B-'+candidate['order'])
        metrics = read(run/'metrics.json')
        if (candidate['results_sha256'] != sha(run/'results.json')
                or candidate['protocol_sha256'] != sha(run/'protocol.json')
                or candidate['macro_f1'] != metrics['macro_f1'] or candidate['nll'] != metrics['calibration']['nll']
                or candidate['p95_intent_seconds'] != metrics['intent_seconds']['p95']):
            raise ValueError('Dev evidence changed after 4B selection')
    groups = ['A', 'D', 'E2', 'C1-4B'] + (['E2-4B'] if selection['include_cascade'] else [])
    identities = {}
    for group in groups:
        args = group_args(study, group)
        if group == 'A':
            args.adapter = None
        identities[group] = evaluation_identity(args, read(args.config) if args.config else None)
    plan = {'schema_version': 1, 'panel_sha256': sha(ROOT/'datasets/intent_routing/v3.json'),
            'source_files': source_manifest(), 'four_b_selection': selection, 'groups': identities,
            'selection_artifacts': selection_artifacts(study),
            'remote_budget_per_group': LIMITS, 'warmup_count': 5,
            'policy': 'One immutable first run per group, failures in denominator; no v3 tuning; no task execution.'}
    write_atomic(out, plan)
    print({'plan': str(out), 'sha256': sha(out), 'groups': groups})


def selection_artifacts(study):
    study = study.resolve()
    paths = [ROOT/'config/decision_router.json', ROOT/'config/decision_router_c1_4b.json',
             study/'four-b-selection.json', study/'calibration-C1-4B/selection.json',
             ROOT/'docs/research/decision-router-results-20261018.json']
    paths += [study/f'dev-C1-4B-{order}'/name for order in ('forward', 'reverse', 'average')
              for name in ('results.json', 'protocol.json', 'metrics.json')]
    return {(p.relative_to(study) if p.is_relative_to(study) else p.relative_to(ROOT)).as_posix(): sha(p) for p in paths}


def paired_no_bootstrap(cases, first, second):
    def correct(rows):
        indexed = {r['id']: r for r in rows}
        return [not indexed.get(c['id'], {}).get('error') and
                (indexed.get(c['id'], {}).get('response') or {}).get('intent') == c['expected_intent'] for c in cases]
    a, b = correct(first), correct(second)
    gain, loss = sum(x and not y for x, y in zip(a, b)), sum(y and not x for x, y in zip(a, b))
    n = gain+loss
    p = min(1., 2*sum(math.comb(n, k) for k in range(min(gain, loss)+1))/2**n) if n else 1.
    return {'n': len(cases), 'accuracy_difference_e2_minus_a': (sum(a)-sum(b))/len(cases),
            'e2_only_correct': gain, 'a_only_correct': loss, 'mcnemar_exact_two_sided_p': p}


def summarize_parts(cases, rows):
    from evals.run_intent_routing_eval import score
    indexed = {r['id']: r for r in rows}
    result = {}
    for part in ('regular', 'auto_boundary', 'colloquial'):
        selected = [c for c in cases if c['part'] == part]
        result[part] = score(selected, [indexed[c['id']] for c in selected if c['id'] in indexed])
    missing = [c for c in cases if c.get('boundary_kind') == 'missing_topic']
    outcomes = Counter((indexed.get(c['id'], {}).get('response') or {}).get('intent', 'ERROR')
        if not indexed.get(c['id'], {}).get('error') else 'ERROR' for c in missing)
    result['missing_topic'] = {'n': len(missing), 'outcomes': dict(outcomes),
        'cases': [{'id': c['id'], 'predicted': (indexed.get(c['id'], {}).get('response') or {}).get('intent'),
                   'error': indexed.get(c['id'], {}).get('error', 'missing' if c['id'] not in indexed else None)} for c in missing]}
    return result


def cost_summary(protocol, rows, *, remote):
    from research_agent.usage import summarize_usage
    warm = protocol.get('remote_warmup_results', [])
    records = [u for r in rows+warm for u in r.get('usage', [])]
    usage = summarize_usage(records)
    observed = sum(r.get('attempts', 0) for r in rows+warm)
    if usage['requests'] != observed:
        raise ValueError('Request and usage receipt count mismatch')
    budget = protocol.get('budget_final')
    if remote:
        if budget is None and (not protocol.get('run_error') or observed):
            raise ValueError('Remote receipts require a budget, except zero-request initialization failure')
        if budget and (budget['attempts_admitted'] > 200 or budget['accounted_tokens_including_unknown_reservations'] > 1_000_000
                or budget['pending_reserved_tokens']):
            raise ValueError('Remote budget violation')
        if len(warm) != 5 and not protocol.get('run_error'):
            raise ValueError('Completed remote runs require five warmups')
    admitted = budget['attempts_admitted'] if budget else observed
    if observed > admitted:
        raise ValueError('Request receipts exceed admitted budget')
    missing = admitted-observed
    unknown = sum(u.get('total_tokens') is None for u in records)
    if budget and not missing and not unknown and not budget['unknown_usage_attempts']:
        if budget['accounted_tokens_including_unknown_reservations'] != (usage['total_tokens'] or 0):
            raise ValueError('Reported token receipts disagree with budget')
    return {**usage, 'requests': None if missing else observed, 'recorded_requests': observed,
            'admitted_requests': admitted, 'admissions_without_receipts': missing,
            'request_count_complete': not missing, 'reported_tokens_complete': not missing and not unknown
            and not (budget or {}).get('unknown_usage_attempts', 0)}


def report(study, destination):
    from evals.build_intent_routing_v3 import verify_frozen
    from evals.run_decision_router_eval import summarize, source_manifest
    if destination.with_suffix('.json').exists() or destination.with_suffix('.md').exists():
        raise FileExistsError(destination)
    panel, plan = verify_frozen(), read(study/'test-plan.json')
    panel_sha, plan_sha = sha(ROOT/'datasets/intent_routing/v3.json'), sha(study/'test-plan.json')
    if (plan['panel_sha256'] != panel_sha or plan['source_files'] != source_manifest()
            or plan['selection_artifacts'] != selection_artifacts(study)):
        raise ValueError('Dataset, source or selection artifacts changed after plan freeze')
    slots = ROOT/'data/decision-router-holdout'/panel_sha
    if read(slots/'plan.json') != {'plan_sha256': plan_sha, 'plan': plan}:
        raise ValueError('Published first-run plan mismatch')
    cases, groups = panel['cases'], {}
    for group, identity in plan['groups'].items():
        run = study/('test-'+group)
        protocol, rows = read(run/'protocol.json'), read(run/'results.json')
        slot = read(slots/(group+'.json'))
        if (protocol['evaluation_identity'] != identity or protocol['source_files'] != plan['source_files']
                or protocol['plan_sha256'] != plan_sha or protocol['panel_sha256'] != panel_sha
                or slot['plan_sha256'] != plan_sha or Path(slot['out']).resolve() != run.resolve()):
            raise ValueError('First-run protocol mismatch: '+group)
        if protocol.get('results_sha256') != sha(run/'results.json'):
            raise ValueError('Recorded first-run results changed: '+group)
        if len(rows) != 160 or {r['id'] for r in rows} != {c['id'] for c in cases}:
            raise ValueError('All 160 result records, including failures, required')
        metrics = summarize(cases, rows, protocol['temperature'], cascade=group.startswith('E2'))
        parts = summarize_parts(cases, rows)
        metrics['parts'] = parts
        metrics['auto_confirm_count'] = sum(r.get('decision', {}).get('fallback_reason') == 'auto_research_confirm' for r in rows)
        cost = cost_summary(protocol, rows, remote=group in {'A', 'E2', 'E2-4B'})
        metrics['initialization_or_run_error'] = protocol.get('run_error')
        groups[group] = {'protocol': protocol, 'rows': rows, 'metrics': metrics,
                         'cost_including_warmup': cost,
                         'results_sha256': sha(run/'results.json'), 'protocol_sha256': sha(run/'protocol.json')}
    pair = paired_no_bootstrap(cases, groups['E2']['rows'], groups['A']['rows'])
    old = ROOT/'docs/research/decision-router-results-20261018.json'
    impact = confirmation_impact(read(old))
    impact['source_sha256'] = sha(old)
    dev = {order: {'protocol': read(study/f'dev-C1-4B-{order}/protocol.json'),
                  'rows': read(study/f'dev-C1-4B-{order}/results.json'),
                  'metrics': read(study/f'dev-C1-4B-{order}/metrics.json')} for order in ('forward', 'reverse', 'average')}
    config4 = read(ROOT/'config/decision_router_c1_4b.json')
    bundle = {'schema_version': 1, 'panel_sha256': panel_sha, 'test_plan_sha256': plan_sha, 'test_plan': plan,
              'auto_confirmation_impact': impact, 'groups': groups, 'paired_e2_minus_a': pair,
              'four_b_dev_runs': dev, 'four_b_calibration': read(study/'calibration-C1-4B/selection.json'),
              'configs': {'D_E2': read(ROOT/'config/decision_router.json'), 'C1-4B': config4},
              'data_review': panel['label_audit'], 'dedup': panel['dedup'],
              'model_provenance': {'id': 'Qwen/Qwen3-4B', 'revision': '1cfa9a7208912126459214e8b04321603b3df60c',
                 'license': 'Apache-2.0', 'url': 'https://huggingface.co/Qwen/Qwen3-4B', 'dtype': 'bfloat16', 'enable_thinking': False},
              'limitations': ['本集由独立盲态代理编写与复核，非人工标注；规模160题且场景仍有相关性。',
                 '测试分布为产品意图与边界场景，不代表真实流量，也不衡量后续研究任务成功率。',
                 '本地GPU与远程API硬件、并发和网络不同；时延与token仅代表这次部署观测，不等于同硬件加速或货币成本。',
                 '只用一个LoRA种子，未补多种子或KL消融；4B顺序和校准只由dev_v2决定。',
                 'LLM确认仍可能错误授权；本集零误触发也不构成生产安全保证，默认保持llm。',
                 '每组只运行一次，失败计错；McNemar只描述本集配对差异，不能证明总体等效。']}
    def pct(v):
        return '—' if v is None else f'{100*v:.2f}%'
    def num(v):
        return '—' if v is None else f'{v:.3f}'
    def fraction(m):
        return f'{m["correct"]}/{m["n"]}（{pct(m["accuracy"])}）'
    a, e = groups['A']['metrics'], groups['E2']['metrics']
    selection = plan['four_b_selection']
    order_cn = {'forward': '正序', 'reverse': '逆序', 'average': '概率平均'}[config4['order']]
    lines = [f'E2 准确率 {fraction(e)}，原 LLM（A）为 {fraction(a)}；AUTO_RESEARCH 误触发分别为 {e["auto_research_false_positives"]} 和 {a["auto_research_false_positives"]} 次。',
             f'4B 免训练开发集最优为{order_cn}顺序，macro-F1 {pct(selection["selected"]["macro_f1"])}；0.6B LoRA 为 {pct(selection["d_macro_f1"])}，'+('已按预定规则加入4B级联。' if selection['include_cascade'] else '因此4B只作对照，未加入级联。'),
             '默认仍为原 LLM 路由；是否切换由用户决定。统一确认减少本地固定意图造成的风险，但不能保证自动研究永不误触发。', '',
             '# 决策路由后续实验', '', '## 主结果', '',
             '|组|准确率|macro-F1|常规105题|边界35题|口语20题|AUTO误触发|非CHAT→CHAT|失败|ECE|回退率|AUTO确认次数|',
             '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for group, data in groups.items():
        m = data['metrics']
        lines.append('|'+ '|'.join([group, fraction(m), pct(m['macro_f1']),
            *(fraction(m['parts'][p]) for p in ('regular', 'auto_boundary', 'colloquial')),
            f'{m["auto_research_false_positives"]}/{m["auto_research_negative_cases"]}', str(m['non_chat_predicted_chat']),
            str(m['errors_or_missing']), num(m['calibration']['ece']) if group in {'D','C1-4B'} else '—',
            pct(m.get('cascade', {}).get('fallback_rate')), str(m['auto_confirm_count'])])+'|')
    lines += ['', 'D 与 C1-4B 仅做本地意图判定；E2 为0.6B LoRA加完整LLM确认。ECE使用开发集冻结温度，失败始终留在准确率分母；ECE只覆盖有概率输出的题。']
    lines += [f'{group} 运行失败：{data["protocol"]["run_error"]}；未开始阶段记为无观测，全部失败保留。'
              for group, data in groups.items() if data['protocol'].get('run_error')]
    lines += ['',
              '## 时延与成本', '', '|组|加载冷启动/秒|首个本地预热/秒|首个远程预热路由/秒|热态意图p50/p95/秒|热态完整路由p50/p95/秒|峰值显存/GiB|远程请求（含预热）|已报告token（含预热）|',
              '|---|---|---|---|---|---|---|---|---|']
    for group, data in groups.items():
        p, m, cost = data['protocol'], data['metrics'], data['cost_including_warmup']
        local_warm, remote_warm = p.get('local_warmup_seconds', []), p.get('remote_warmup_results', [])
        requests = str(cost['requests']) if cost['request_count_complete'] else f'未知（收据{cost["recorded_requests"]}次，另有{cost["admissions_without_receipts"]}次准入待核对）'
        tokens = cost['total_tokens'] if cost['recorded_requests'] else 0
        token_text = f'{tokens:,}' if tokens is not None else '未知'
        if not cost['reported_tokens_complete']:
            token_text = '不完整（已记录'+token_text+'）'
        lines.append('|'+ '|'.join([group, num(p.get('cold_start_seconds')), num(local_warm[0] if local_warm else None),
            num(remote_warm[0].get('route_seconds') if remote_warm else None),
            num(m['intent_seconds']['p50'])+'/'+num(m['intent_seconds']['p95']),
            num(m['route_seconds']['p50'])+'/'+num(m['route_seconds']['p95']),
            num(p['peak_gpu_memory_bytes']/1024**3) if p.get('peak_gpu_memory_bytes') else '—',
            requests, token_text])+'|')
    lines += ['', '硬件为 RTX 4080 SUPER 16GB，本地模型使用bf16、关闭thinking且不量化。冷启动是本进程模型加载（含身份校验）；首个预热样本只作单点观测，不计算冷态分位数。热态在5次预热后计时，级联还包含排队、字段补全或完整回退；本地分类组不生成完整路由，故该列留空。每个远程组预算900秒、200次请求、100万已报告token。', '',
              '## 改动一：统一AUTO确认与离线复算', '',
              'cascade中本地top-1为AUTO_RESEARCH时直接调用完整route_intent，允许改判CHAT，记录auto_research_confirm；不再使用tau_auto或固定AUTO字段补全。其他意图及decision路径保持原有行为。', '']
    for split, info in impact['splits'].items():
        lines.append(f'- {split}：{info["affected_cases"]}题曾过门槛进入AUTO字段补全，最终直接接受{info["finally_accepted_cases"]}题；新规则新增完整确认{info["full_route_calls_added"]}次、去掉字段补全{info["field_completion_calls_removed"]}次，忽略重试的远程调用净增{info["expected_remote_request_delta_without_retries"]}次。')
    lines += ['', '旧记录中上述字段补全全部因预算字段校验失败而回退；v1那次误触发最终来自完整LLM路由，设计稿中的路径推断已更正。以上仅复算受影响题数与调用数，不推导新的准确率或token成本。', '',
              '## 无主题授权与边界错题', '']
    for group, data in groups.items():
        m = data['metrics']
        counts = m['parts']['missing_topic']['outcomes']
        lines.append(f'- {group}：无主题授权15题的最终结果为'+ '、'.join(f'{k} {v}题' for k,v in sorted(counts.items()))+'。')
        boundary_ids = {c['id'] for c in cases if c['part'] == 'auto_boundary'}
        flagged = [f for f in m['failures'] if f['id'] in boundary_ids or f['predicted'] == 'AUTO_RESEARCH']
        lines.append('  边界错题或AUTO误触发：'+('；'.join(f'{f["id"]}（{f["expected"]}→{f["predicted"]}）' for f in flagged) or '无')+'。')
    lines += ['', '## 配对比较与复现', '',
              f'E2−A准确率差为 {100*pair["accuracy_difference_e2_minus_a"]:+.2f} 个百分点；E2单独正确 {pair["e2_only_correct"]} 题，A单独正确 {pair["a_only_correct"]} 题。McNemar精确双侧检验 p={pair["mcnemar_exact_two_sided_p"]:.6g}，未做bootstrap。', '',
              f'v3 SHA-256：`{panel_sha}`；测试计划 SHA-256：`{plan_sha}`。独立作者仅接触标签政策、产品规则和场景标签；另一代理在不看预测的条件下逐题复核。search_scope在每类内均衡；与v1/v2全集及v3内部完成精确、5-gram Jaccard≥0.6、BGE-M3余弦≥0.92去重，记录保存在数据和JSON。', '',
              '4B固定版本为`Qwen/Qwen3-4B@1cfa9a7208912126459214e8b04321603b3df60c`，Apache-2.0。与0.6B的选项提示哈希一致；三个顺序的开发记录、温度及阈值选择、原始逐题结果与全部错误ID见同名JSON。原v2报告未修改。', '',
              '## 局限', ''] + ['- '+note for note in bundle['limitations']]
    write_atomic(destination.with_suffix('.json'), bundle)
    destination.with_suffix('.md').write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')
    print(str(destination.with_suffix('.md')))


def select_four_b(study, config_out):
    from evals.calibrate_decision_router import calibrate
    from evals.build_intent_routing_v2 import verify_frozen
    from evals.run_decision_router_eval import summarize
    from research_agent.decision_router import prompt_hash
    if (study/'four-b-selection.json').exists() or config_out.exists():
        raise FileExistsError('4B selection/config is already frozen')
    previous = ROOT/'evals/reports/decision-router-20261017'
    reference = read(previous/'dev-D-average/metrics.json')
    cases = [c for c in verify_frozen()['cases'] if c['split'] == 'dev_v2']
    expected_ids, weights = {c['id'] for c in cases}, None
    candidates = []
    for order in ('forward', 'reverse', 'average'):
        run = study/('dev-C1-4B-'+order)
        protocol, metrics = read(run/'protocol.json'), read(run/'metrics.json')
        if (protocol.get('split') != 'dev_v2' or protocol.get('order') != order or protocol.get('group') != 'C1'
                or protocol.get('model') != 'Qwen3-4B' or protocol.get('adapter_sha256') is not None
                or protocol.get('adapter_path') or protocol.get('temperature') != 1.
                or protocol.get('panel_sha256') != sha(ROOT/'datasets/intent_routing/v2.json')
                or protocol.get('prompt_sha256') != prompt_hash() or not protocol.get('weights_sha256')):
            raise ValueError('4B dev identity or order mismatch')
        if weights is not None and weights != protocol['weights_sha256']:
            raise ValueError('4B model weights differ between orders')
        weights = protocol['weights_sha256']
        rows = read(run/'results.json')
        if (len(rows) != len(cases) or {r['id'] for r in rows} != expected_ids
                or any(r.get('error') or r.get('logits') is None for r in rows)):
            raise ValueError('Complete dev probabilities required for every order')
        recomputed = summarize(cases, rows)
        if metrics != recomputed:
            raise ValueError('Dev metrics do not match raw predictions')
        candidates.append({'order': order, 'macro_f1': metrics['macro_f1'], 'nll': metrics['calibration']['nll'],
                           'p95_intent_seconds': metrics['intent_seconds']['p95'],
                           'results_sha256': sha(run/'results.json'), 'protocol_sha256': sha(run/'protocol.json')})
    selected = min(candidates, key=lambda c: (-c['macro_f1'], c['nll'], c['p95_intent_seconds']))
    run = study/('dev-C1-4B-'+selected['order'])
    calibrate(run, study/'calibration-C1-4B')
    config = read(study/'calibration-C1-4B/config.json')
    if config['prompt_sha256'] != read(previous/'dev-C1-average/protocol.json')['prompt_sha256']:
        raise ValueError('4B and 0.6B scoring prompt mismatch')
    write_atomic(config_out, config)
    result = {'rule': 'highest dev_v2 macro-F1, then lowest NLL, then lowest p95 intent latency',
              'candidates': candidates, 'selected': selected, 'd_macro_f1': reference['macro_f1'],
              'd_results_sha256': sha(previous/'dev-D-average/results.json'),
              'cascade_rule': 'include E2-4B iff selected 4B dev_v2 macro-F1 is strictly greater than D',
              'include_cascade': selected['macro_f1'] > reference['macro_f1'],
              'config_sha256': sha(config_out), 'selection_split': 'dev_v2', 'test_v3_predictions_read': False}
    write_atomic(study/'four-b-selection.json', result)
    print(result)


def confirmation_impact(bundle):
    result = {}
    for split, groups in [('test_v2', bundle['groups']), ('regression_v1', bundle['regression_v1'])]:
        rows = groups['E']['rows']
        auto = [r for r in rows if r.get('decision', {}).get('intent') == 'AUTO_RESEARCH']
        affected = [r for r in auto if r['decision'].get('field_completion')]
        accepted = [r for r in auto if r['decision'].get('fallback') is False]
        if any(not r['decision'].get('field_completion') for r in accepted):
            raise ValueError('Accepted AUTO must have used field completion in the original study')
        result[split] = {'local_auto_cases': len(auto), 'affected_cases': len(affected),
                        'affected_ids': [r['id'] for r in affected], 'finally_accepted_cases': len(accepted),
                        'full_route_calls_added': len(accepted), 'field_completion_calls_removed': len(affected),
                        'expected_remote_request_delta_without_retries': len(accepted)-len(affected)}
    return {'splits': result, 'assumption': 'Affected means locally accepted into field completion before field '
            'validation. Some already fell back to full routing after that completion failed. The new rule '
            'removes those completions and adds a full route only for finally accepted cases. '
            'Actual HTTP retries and token changes require a new run; no new accuracy is inferred.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument('--impact-from', type=Path)
    commands.add_argument('--select-four-b', type=Path, help='Study directory with three complete 4B dev_v2 runs')
    commands.add_argument('--prepare-plan', type=Path, help='Freeze the dev selection and all v3 evaluation identities')
    commands.add_argument('--study', type=Path, help='Summarize completed first-run v3 groups')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.select_four_b:
        select_four_b(args.select_four_b, args.out)
        return
    if args.prepare_plan:
        prepare_plan(args.prepare_plan, args.out)
        return
    if args.study:
        report(args.study, args.out)
        return
    if args.out.exists():
        raise FileExistsError(args.out)
    result = confirmation_impact(read(args.impact_from))
    result['source_sha256'] = sha(args.impact_from)
    write(args.out, result)
    print(result)


if __name__ == '__main__':
    main()
