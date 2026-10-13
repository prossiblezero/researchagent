"""Summarize every frozen attempt, including unknown usage and incomplete runs."""
import argparse
import json
from pathlib import Path

from prepare_short_research import read, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    protocol = read(args.run/'packets/protocol.json')
    rows = []
    for task, arm in protocol['run_order']:
        path = args.run/f'{task}-{arm}'/'result.json'
        if not path.exists():
            rows.append({'task': task, 'arm': arm, 'pending': True})
        else:
            row = read(path)
            row['pending'] = False
            rows.append(row)
    write(args.run/'summary.json', {'protocol': protocol, 'attempts': rows, 'scientific_effect_verified': False})
    lines = ['# Auto Research 与直接 Codex：短任务筛查', '',
             '同一组公开开发资料、固定 BM25 基线、隔离留出评分。每格一次自主尝试；每格总上限 360 秒和 400,000 输入（含缓存）加输出 token。主控与编码合计。', '',
             '成功要求：在预算内完成、计量完整、提交有效、留出宏平均 evidence recall 绝对提升至少 2 个百分点。费用未知不会按零计算，未完成或超限提交即使可评分也不计成功。', '',
             '| 任务 | 系统 | 完成 | 有效提交 | 留出基线 | 留出提交 | 提升（百分点） | 成功 | 秒 | 已报告 token | 计量完整 |',
             '| --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- |']
    if all(not r['pending'] for r in rows) and not any(r['passed'] for r in rows):
        lines[2:2] = ['**本轮没有证明当前 Auto Research 优于直接 Codex，也没有证明此前优化提高了短任务成功率。三组预算内成功均为 0/3。** 失败主要发生在交付和资源计量阶段，不能把这一结果解释为方法本身一定没有效果。', '']
    labels = {'before': '优化前', 'after': '当前版本', 'codex': '直接 Codex CLI'}
    for r in rows:
        if r['pending']:
            lines.append(f"| {r['task']} | {labels[r['arm']]} | 待完成 | — | — | — | — | — | — | — | — |")
            continue
        u, s = r['usage'], r['score']
        held = s.get('held_out')
        scores = f"{held['baseline']['recall_at_k']:.4f} | {held['candidate']['recall_at_k']:.4f} | {held['gain']*100:+.2f}" if held else '— | — | —'
        tokens = str(u.get('reported_tokens', '未知')) if u.get('usage_complete') else ('未知（已报告下界 '+str(u.get('reported_tokens', 0))+'）')
        lines.append(f"| {r['task']} | {labels[r['arm']]} | {bool(u.get('completed'))} | {s['valid_submission']} | {scores} | {r['passed']} | {u.get('seconds', '未知')} | {tokens} | {u.get('usage_complete', False)} |")
    lines += ['', '## 按系统汇总', '', '| 系统 | 成功/已运行 | 有效提交 | 已报告 token 合计 | 计量完整尝试 |', '| --- | ---: | ---: | ---: | ---: |']
    for arm in labels:
        selected = [r for r in rows if r['arm']==arm and not r['pending']]
        lines.append(f"| {labels[arm]} | {sum(r['passed'] for r in selected)}/{len(selected)} | {sum(r['score']['valid_submission'] for r in selected)} | {sum(r['usage'].get('reported_tokens') or 0 for r in selected):,} | {sum(bool(r['usage'].get('usage_complete')) for r in selected)}/{len(selected)} |")
    lines += ['', 'token 合计是提供方已报告部分；任何缺失使实际总费用只能视为下界。不同任务的 recall@k 不合并成一个模型能力分数。', '', '## 失败与声明核对', '']
    for r in rows:
        if r['pending']:
            continue
        u, s = r['usage'], r['score']
        lines.append(f"- **{r['task']} / {labels[r['arm']]}**：状态 `{u.get('status', 'exception')}`；"
                     + (f"开发分数声明误差 {s.get('dev_claim_error', 0):.6g}，错误正向开发声明 {s.get('unsupported_positive_dev_claim')}。" if s['valid_submission'] else f"{s.get('error', '提交无效')}")
                     + f" [原始结果]({r['task']}-{r['arm']}/result.json)")
        for cli in u.get('cli', []):
            receipt = cli.get('receipt', {})
            lines.append(f"  编码会话：{receipt.get('termination', 'unknown')}，{receipt.get('seconds', '未知')} 秒，{receipt.get('tool_calls', '未知')} 次工具动作。")
    lines += ['', '## 已验证的方法效果与下一处阻塞', '']
    for r in rows:
        if r['pending'] or not r['score']['valid_submission']:
            continue
        held = r['score']['held_out']
        diffs = [a-b for a,b in zip(held['candidate']['per_case'], held['baseline']['per_case'])]
        lines.append(f"{r['task']} / {labels[r['arm']]} 的冻结方法在 {len(diffs)} 个留出问题上："
                     f"{sum(d>0 for d in diffs)} 个改善、{sum(d==0 for d in diffs)} 个持平、{sum(d<0 for d in diffs)} 个下降。"
                     f"平均提升 {held['gain']*100:.2f} 个百分点，开发分数声明误差为 {r['score']['dev_claim_error']:.6g}。预算是否合规仍按上表单独判定。")
        for cli in r['usage'].get('cli', []):
            for usage in cli['usage']:
                lines.append(f"该会话输入 {usage.get('input_tokens')}、其中缓存输入 {usage.get('cached_input_tokens', '未知')}、输出 {usage.get('output_tokens')} token；缓存量已包含在输入中，没有重复相加。")
    lines += ['', '本轮 Auto Research 未获得有效最终提交。优先处理的工程问题是：子任务额度过小导致首次编码失败、截止前缺少可恢复提交，以及 CLI 中断后缺少可核验的累计用量。完成这些基础能力后，再按新协议检验反馈恢复带来的增益；不应继续仅用提示词或单步动作通过率宣称科研效果提升。']
    lines += ['', '## 解释边界', '',
              '- 这是 3 个短研究任务、每系统每任务 1 次筛查，不能给出可靠显著性结论或完整科研成功率。LoCoMo 每个 split 只有两个会话，问题彼此相关。',
              '- 使用 api.sudocode.chat 配置的 gpt-5.6-luna 别名；主控为 Chat Completions 默认推理设置，编码和直接组为 Codex Responses medium reasoning。底层权重未核实，存在接口与推理设置混杂。',
              '- Windows 原生启动与工具开销纳入自主预算。单次请求可能越过剩余 token；超限必须保留并判失败。CLI 只提供会话终结用量，底层请求数无法独立核验。',
              '- 已启动的 CLI 一旦缺失终结用量，严格计量适配器会停止整次尝试。因而这类失败没有覆盖产品后续恢复环节，不能据此判定反馈记忆优化无效。',
              '- 第一格 qasper-before 与约 152 秒的离线回归存在重叠，未严格隔离整机 CPU。该限制尤其影响精细耗时比较；完整情况记录于 run-notes.json。',
              '- 独立复测每个 split 最多 120 秒，不反馈给 Agent，不占自主预算；三组使用相同规则。开发和留出分别从同一冻结源码创建只读沙箱，宿主只从预测重新计算指标。',
              '- 资料为现有公开数据的新子集，数据来源、排除规则、样本 ID、输入哈希与运行顺序在 [冻结协议](packets/protocol.json)。不根据 Agent 成绩选样本或改门槛。',
              '- 判断本次产品优化，需要比较 before 与 after；判断编排收益，需要比较 after 与 codex。缺失有效交付时不能将结果解读为算法本身较差。', '']
    (args.run/'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'completed': sum(not r['pending'] for r in rows), 'total': len(rows), 'report': str(args.run/'report.md')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
