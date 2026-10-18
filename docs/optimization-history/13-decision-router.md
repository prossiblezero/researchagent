# 13 · 决策模型式意图路由：本地小模型选择，LLM 负责生成与确认

时间：2026-10-16—2026-10-18。把七类意图路由从“每条消息调用一次远程 LLM”改为“本地决策模型先打分、低置信与自动研究交 LLM 确认”的级联，并在两套预先冻结的保留集上分别评测。默认仍为原 LLM 路由（`ROUTER_MODE=llm`），级联需要显式开启。

## 原路由暴露的问题

2026-10-12 的 210 题评测（`datasets/intent_routing/v1.json`）显示，原路由准确率较高，但有三个结构性问题：

| 现象 | 数据 | 原因 |
| --- | --- | --- |
| 时延高 | 单题 p50 14.07 秒，p95 45.44 秒；另有 4 题超时 | 一次调用同时输出 `intent/reply/brief/assumptions/effort` 五个字段，其中只有 intent 是分类问题 |
| 错误集中流向 CHAT | 9 道分类错误全部被判为 CHAT，EXPERIMENT 召回 25/30 | 只输出一个标签，看不到模型的偏置有多强 |
| 无法“不确定时回退” | 没有置信度 | 生成式输出没有可校准的概率 |

LOCAL_QA、DOWNLOAD、BRAINSTORM、EXPERIMENT 四类意图入队时根本不需要生成文字，却和 CHAT、RESEARCH 一样等待完整生成。

## 设计：决策模型做选择，LLM 做生成

思路来自 TypeSafe Jev 一类“决策模型”：对声明好的选项返回概率分布，而不生成文本。项目没有 Jev API，实现参考 [LLM-as-Jev（arXiv 2610.02076）](https://arxiv.org/html/2610.02076v2) 的选项打分方法，用本地开源小模型自建，不声称使用或对比了 Jev 本身。

1. **选项打分。** 七类意图按固定顺序编号 `[1]…[7]`，选项描述直接取数据集的 `label_policy`。用 chat template 关闭 thinking，预填 `最佳选项：[`，对每个候选累加 `k]` 两个 token 的 log-prob 再做 softmax。prompt 只 prefill 一次，七个候选共享 KV cache。
2. **顺序去偏。** 联合打分对选项顺序敏感，正序、逆序和两者按意图对齐后的概率平均三种方案只在开发集上选择，最终选平均。
3. **LoRA 微调。** 训练目标与推理打分一致：七个候选上的 listwise 交叉熵，加上相对冻结基座的 KL 锚定（λ=1），以减少只用意图数据微调带来的捷径和过度自信。LoRA rank 16、alpha 32，作用于 q/k/v/o；三个学习率各训练 1 epoch，每 1/4 epoch 存一次检查点。选模看开发集 macro-F1，并要求 ECE 不高于免训练版 +0.03，不只看验证 loss。
4. **校准与阈值。** 开发集上拟合温度缩放；在 risk–coverage 网格上选 `p_max`、`margin` 阈值。
5. **级联接入。** `research_agent/decision_router.py` 返回与 `route_intent()` 同形的五字段结果，经过同样的 `_parse_route` 校验，`Workbench` 的分发逻辑不变。高置信的四类无文本意图直接构造路由；CHAT、RESEARCH 固定意图后由 LLM 只补 reply 或 brief；低置信、打分超时、身份校验失败或字段补全异常，都回退原 `route_intent()`。trace 增加 `intent_decision` 事件，记录七类概率、是否回退和原因。

## 第一轮：v2 保留集（140 题）

v1 的 210 题已经看过结果并据此分析失败，按数据集约定只能作回归集。因此先新建 `datasets/intent_routing/v2.json`：训练 1,400 条（每类 40 个场景族、各 5 个主题变体）、开发 70 题、保留测试 140 题。三部分由不同代理编写，另一代理在不看预测的条件下复核；训练集与开发、测试及 v1 全集做精确、5-gram Jaccard 和 BGE-M3 余弦去重。数据冻结后才允许任何模型运行，每组只在测试集上运行一次，失败计错。

| 组 | 方法 | 准确率 | macro-F1 | AUTO 误触发 | 意图判定 p50 |
| --- | --- | ---: | ---: | ---: | ---: |
| A | 原 LLM 路由 | 136/140（97.14%） | 97.82% | 0/120 | 6.81s |
| B | BGE-M3 + 逻辑回归 | 86/140（61.43%） | 61.26% | 0/120 | 0.013s |
| C0 | 0.6B 贪心生成意图名 | 21/140（15.00%） | 5.61% | 1/120 | 0.086s |
| C1 | 0.6B 免训练选项打分 | 41/140（29.29%） | 23.93% | 16/120 | 0.20s |
| D | 0.6B LoRA 选项打分 | 135/140（96.43%） | 96.34% | 2/120 | 0.24s |
| E | D + LLM 回退 | 136/140（97.14%） | 97.49% | 0/120 | 0.47s |

LoRA 版本的 ECE 为 0.041，CHAT 预测数从免训练的 98 降到 16，没有出现过度自信或偏向某类的捷径。级联的完整路由 p50 为 1.07 秒，回退率 25%，远程请求 148→83（均含预热）。

问题出在 v1 回归上：级联有 1/184 次 AUTO_RESEARCH 误触发，原路由为 0/184。逐题核对发现，小模型容易把“授权了自动研究但没给研究主题”判成 AUTO_RESEARCH，而产品规则要求这类请求先走 CHAT 澄清。AUTO_RESEARCH 是唯一会自动执行代码的意图，这个差距足以阻止默认切换。

## 第二轮：AUTO_RESEARCH 统一交 LLM 确认

改动（`6ef0bef3`）：本地模型 top-1 为 AUTO_RESEARCH 时，不再使用专用阈值和固定意图的字段补全，直接调用完整 `route_intent()`，由 LLM 自由判定，允许改判 CHAT；trace 记 `auto_research_confirm`。其他意图的快速路径不变。理由是自动研究一次就要运行数分钟到数小时，路由多等几秒影响很小，误触发的代价却是启动代码执行。

逐题复核还纠正了设计稿中的一处推断：v1 那次误触发并不是“固定意图后没机会改判”，而是字段补全失败后回退到完整 LLM，LLM 自己返回了 AUTO_RESEARCH。统一确认的改法不受影响，但它降低的是本地模型带来的风险，不能保证 LLM 永不误判。

同时补了一组 4B 免训练打分（`Qwen/Qwen3-4B`），提示模板与 0.6B 完全一致。开发集最优为逆序，macro-F1 67.96%，低于 0.6B LoRA 的 94.03%，按事先规则只作对照，不进入级联。

## 第三轮：全新 v3 保留集（160 题）

改动是在看过 v2 结果之后做的，不能再用 v2 证明。`datasets/intent_routing/v3.json` 只含测试题：常规 105 题（七类各 15）、自动研究边界 35 题、口语化请求 20 题。编写者不接触 v2 测试题、错题和任何预测；search_scope 在各类内均衡，避免 v2 中 RESEARCH/DOWNLOAD 多带 web/arxiv 的偏差；与 v1、v2 全集做三种去重。运行计划在任何 v3 推理前冻结为四组，每组一次。

| 组 | 准确率 | 常规 | 边界 | 口语 | AUTO 误触发 | 非 CHAT→CHAT |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A 原 LLM 路由 | 156/160（97.50%） | 96.19% | 100% | 100% | 0/134 | 3 |
| D 0.6B LoRA（纯本地） | 134/160（83.75%） | 96.19% | 51.43% | 75.00% | 15/134 | 1 |
| E2 级联 + AUTO 确认 | 158/160（98.75%） | 99.05% | 100% | 95.00% | 0/134 | 0 |
| C1-4B 免训练（纯本地） | 103/160（64.38%） | 69.52% | 51.43% | 60.00% | 33/134 | 4 |

| 组 | 完整路由 p50 / p95 | 远程请求（含预热） | 已报告 token（含预热） | 峰值显存 |
| --- | ---: | ---: | ---: | ---: |
| A | 6.39s / 27.00s | 168 | ≥488,182（1 次超时用量未知） | — |
| D | 意图判定 0.20s / 0.24s | 0 | 0 | 1.69 GiB |
| E2 | 3.99s / 19.07s | 98 | 218,773 | 1.70 GiB |
| C1-4B | 意图判定 0.12s / 0.13s | 0 | 0 | 8.33 GiB |

15 道“无主题授权”题中，D 和 C1-4B 全部误判为 AUTO_RESEARCH，A 和 E2 全部返回 CHAT。E2 的回退率为 37.5%，其中 40 次是 AUTO_RESEARCH 确认。E2 与 A 的配对差为 +1.25 个百分点（E2 单独正确 4 题，A 单独正确 2 题），McNemar 精确检验 p=0.6875，只能说准确率持平，不能说提升。

本地 GPU 为 RTX 4080 SUPER 16GB，bf16、不量化。时延是“本地小模型 + 远程 API”的部署观测，不是同硬件下的模型速度比较；A 这一轮的 p50 为 6.39 秒，与 10-12 的 14.07 秒相差较大，说明远程服务本身波动明显，跨轮次的时延不直接比较。

## 结论与保留的边界

- **保留**：级联路由（E2）与 AUTO_RESEARCH 统一确认。它在全新保留集上准确率不低于原路由，自动研究误触发保持为 0，完整路由 p50 降低约 38%，远程请求减少 42%，token 至少减少 55%。
- **不采用**：纯本地小模型直接路由。常规题与原路由持平，但边界题只有 51%，无主题授权全部误触发。
- **结论**：这个任务需要有监督适配。0.6B 免训练只有 29%，4B 免训练也只有 64%；0.6B 加 LoRA 在常规题上达到 96%。
- **默认不切换**：`ROUTER_MODE` 默认仍为 `llm`，由使用者显式开启 `cascade`。
- **边界**：数据由代理编写、代理盲审，不是人工标注；场景之间有模板和主题相关性；只训练了一个 seed，没有做 KL 锚定的消融，不能把收益归因于 KL 项。时延收益取决于流量中无需生成文字的意图占比；E2 的 p95 仍有 19 秒，长尾来自回退请求。

## 可追溯记录

- 设计与方案：[原设计](../research/decision-router-design-20261016.md)、[后续方案及变更记录](../research/decision-router-followup-20261018.md)、[实现说明](../research/decision-router.md)。
- 结果：[v2 首次评测](../research/decision-router-results-20261018.md)（含训练曲线与校准图）、[v3 后续评测](../research/decision-router-v3-results-20261018.md)，以及同名 JSON 中的逐题预测、阈值网格和请求哈希。
- 数据：`datasets/intent_routing/v2.json`、`datasets/intent_routing/v3.json`，哈希登记在 `datasets/manifest.json`。
- 代码：`research_agent/decision_router.py`、`research_agent/workbench.py`（`select_router` 接入）、`evals/build_intent_routing_v2.py`、`evals/build_intent_routing_v3.py`、`evals/train_decision_router.py`、`evals/calibrate_decision_router.py`、`evals/run_decision_router_eval.py`、`tests/test_decision_router.py`；配置为 `config/decision_router.json` 与 `config/decision_router_c1_4b.json`。
- 验收：源码导出副本执行 `evals/run_all.py`，750 项测试通过、7 项跳过。模型权重与 LoRA adapter 保存在本地 `data/models/`，不进入仓库。文件指纹见[证据索引](evidence-index.md)，本地提交见[提交索引](commits.md)。
