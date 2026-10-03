# r27 简历证据与面试说明

2026-09-30（北京时间）。r27 在 r26 基础上加入第二个独立 QASPER 课题的结构复核结果，并把“原始验收失败”“修复后结构复核通过”和“科学收益未验证”分开表达。独立审计同时纠正了 QASPER 原报告把 Hit@5 记成 Recall@5 的口径问题。SciFact r2 的源码变化、冻结评分器/数据、独立消融和单 seed 限制全部保留。

## 我理解的项目目标

项目最终要成为一个按需进入 Auto Research 的研究 Agent，而不是把普通问答强行变成自动科研。

普通聊天、论文调研和资料问答时，系统应快速检索、阅读、总结和引用；研究区共享资料、Notes 和设置，区内允许多个独立会话，只有用户显式从历史消息分支时才继承该段历史。

用户明确给出复杂研究目标后，系统才进入：

```text
目标拆解
→ 检索论文/报告并读取原文
→ Claim–Evidence–Source
→ 总结方法、数据集、代码和指标
→ 找研究缺口，提出 HYPOTHESIS
→ 选择 baseline，设计对比与消融
→ 研究 LLM 按需调用 Codex
→ 宿主独立执行和测量
→ 根据真实指标继续调研、改方案、重跑或结束
→ 生成可追溯报告
```

项目本身的价值在证据约束、研究状态、工具编排、实验条件比较、反馈决策、恢复和可查交付；Codex 的编码能力属于外部工具能力。

## 当前完成度与差距

| 目标部分 | 当前状态 | 主要差距 |
| --- | --- | --- |
| 普通聊天、论文调研、资料问答 | 已实现并有真实模型评测 | 继续扩大真实场景覆盖和长文稳定性 |
| 研究区与会话模型 | 已实现 | 持续守住多会话独立与显式分支语义 |
| 原文证据、引用、记忆与压缩 | 已实现基础且有量化结果 | PDF 表格/公式、长文完整阅读、核验超时和引用不足仍有边界 |
| 报告、关系、假设、实验版本 | 已实现基础 | 能保存和追溯，不等于已经证明候选新颖或有效 |
| Auto Research 功能路径 | SciFact r2、QASPER r1 的研究→委派→宿主测量路径已跑通；QASPER 跨轮结构复核通过 | 科学收益仍未验证 |
| 结果驱动方法迭代 | SciFact 完成 2 版方案、真实源码变化和独立消融；QASPER 原始收据经修复后验收器结构复核通过 | 尚未在第二个课题上得到科学指标改进 |
| Codex 作为工具 | 已接通，失败可追溯，跨轮源码可演进 | 需要更多案例验证调用时机、复用和恢复 |
| 宿主管理实验模型 | 接口和隔离已实现 | 尚需真实模型实验跨课题复用 |
| 继续/停止决策 | 有结果回传、比较和失败保留 | 尚未证明多任务下决策稳定 |
| 完全自动科研目标 | 未完成 | 还缺跨数据集、多 seed 的真正随机重复、统计比较、科学审计和更强原文核验 |

目前更准确的工程判断是：研究工作台与 Harness 基础设施约 **80%–90%**，Auto Research 可运行编排约 **70%–80%**，复杂课题稳定自主研究约 **45%–55%**。这些是路线判断，不是科学指标或通过率；QASPER 的结构复核通过不等于候选方法有效。

## SciFact r2 真实证据

- 报告目录：`evals/reports/auto-research-live-20260930-luna-iteration-r2/`。
- 终态：`passed=true`、`functional_path_completed=true`、`feedback_iteration_path_completed=true`、`plan_versions=2`、`research_jobs=3`；其中两个定向子研究失败，原始失败仍保留。
- 实验规模：3 次 Codex 委派、2 次 execution-only 宿主测量任务、6 组有效 measurement；研究模型实际收到宿主结果。
- 方案 v1：baseline accuracy/macro-F1 **0.5700/0.4738**；candidate **0.5600/0.4658**、CONTRADICT F1 **0.0588**。
- 方案 v2：固定相同 `evaluate.py`、`data/scifact`、300 条 dev、`seed=13`；baseline anchor **0.5700/0.4738**，candidate accuracy/macro-F1 **0.5367/0.4739**、CONTRADICT F1 **0.1770**，独立 ablation **0.5167/0.4357**、CONTRADICT F1 **0.0748**。
- 独立字节核对：两版 `evaluate.py` 哈希均为 `b9d94e892f9ee4d9c3a7907129ceb0cd68f599a0930ef6276cf3c4a4fce7e884`；`data/scifact` 保护哈希相同；跨轮只有 `algorithms.py` 改变。
- 真实原生回归：`test_native_two_round_revision_preserves_frozen_scorer_and_data` 通过；后续任务能修改生成源码，试图写 `evaluate.py` 或数据时被 Windows 沙箱拒绝。

## QASPER r1 真实证据

- 数据集：官方 dev 中 **24 条**已完成证据映射且允许检索的任务；固定 seeds `[13, 17, 29]`，baseline、candidate、ablation 均声明并执行完整 seed 集合。
- 功能路径：`functional_path_completed=true`，多 seed 证据门槛通过；控制器完成研究、Codex 委派和宿主测量。
- 原报告结果：首轮 baseline/candidate/ablation 的 `recall_at_5` 为 **0.6250/0.5833/0.6250**；第二轮均为 **0.6250**。独立审计将该字段确认为 Hit@5，并按 gold 段落比例重算严格 Recall@5：首轮 **0.5073/0.4657/0.5073**，第二轮三者均 **0.5073**；MRR@5 保持 **0.2840/0.2736/0.2840**（第二轮均 **0.2840**）。
- 结论：原始 `metrics.json` 为 `feedback_iteration_path_completed=false`、`passed=false`；原因是旧验收器要求跨轮 candidate 与前版保持相同脚本哈希，把真实的方法源码变化误判成评分器变化。修复后针对原始请求、任务和宿主收据的独立结构复核为 `structural_path_completed=true`，确认 `retrieval.py` 变化及第二轮三角色重跑；复核记录保存在 `evals/reports/auto-research-live-20260930-luna-qasper-r1/review-independent/acceptance-recomputed.json`。独立指标审计仍为 `scores_reproduced=false`，因为原报告的 Recall@5 字段实际是 Hit@5；因此 r27 只把 QASPER 作为跨课题功能与失败检测证据，不宣称检索或科学收益。

## 面试短说法

> 我做的是一个按需进入 Auto Research 的证据驱动研究 Agent。普通聊天和调研保持轻量；复杂目标下研究模型读原文、形成假设、选 baseline，并按需调用 Codex；宿主独立执行和测量，再把真实结果交回研究模型决定继续、修改、重跑或结束。现在 SciFact 已跑通源码真实改变、严格消融和反馈驱动的两版方案，QASPER 的跨轮结构路径也通过了修复后的收据复核，但候选没有超过基线，所以我保留失败并把“可运行闭环”和“科学有效性”分开。下一步是第二课题的真实方法收益、真正随机多 seed 和稳定的继续/停止判定。
