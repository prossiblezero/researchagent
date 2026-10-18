# 决策模型式意图路由：后续改进方案

日期：2026-10-18。状态：F1–F4 已实现并通过验收。实现者：Codex。

前置文档：[原设计](decision-router-design-20261016.md)、[首次评测结果](decision-router-results-20261018.md)。本轮只做下面三件事，不补多 seed，不做 KL 消融，不改动已有的 test_v2 成绩和报告。实现中如有偏离，写进文末“变更记录”。

## 0. 目标

1. 消除级联路由的 AUTO_RESEARCH 误触发：v1 回归中有 1/184 次误触发，与原 LLM 路由的 0/184 相比是唯一的安全差距。
2. 补一组 4B 免训练打分，回答“免训练选项打分在更大模型上是否可用”。0.6B 免训练只有 29.29%，主要原因是位置偏差。
3. 在一套全新的保留集 test_v3 上复测。test_v2 的结果已经看过，本轮的改动不能再用它来证明。

## 1. 改动一：AUTO_RESEARCH 一律交给 LLM 确认

### 1.1 问题

首次评测中，小模型把 AUTO_RESEARCH 误触发到其他意图上，D 组 v2 有 2 次，v1 有 5 次；级联 E 组在 v1 上有 1 次（`test-auto_research-22`）。大部分属于同一类情况：用户授权了自动研究，但没有给出研究主题，按产品规则应当返回 CHAT 先澄清。

现有级联在小模型判为 AUTO_RESEARCH 且超过 `tau_auto` 时，会进入 `complete_route_fields`。这一步固定了意图，LLM 只补 brief，没有机会改判成 CHAT。

### 1.2 改法

在 `research_agent/decision_router.py: DecisionRouter.route()` 的 cascade 分支中修改：

- 小模型 top-1 为 AUTO_RESEARCH 时，**不再使用 `tau_auto`，也不调用 `complete_route_fields`**，直接调用完整的 `route_intent(model, history, space, search_scope)`，由 LLM 自由判定意图。trace 中记 `fallback=True`、`fallback_reason='auto_research_confirm'`。
- 其他意图的流程不变：高置信的四类直接构造结果，CHAT/RESEARCH 走字段补全，低置信回退。
- `decision` 模式只用于评测，保持原样，作为纯本地对照。

理由：只有小模型判成 AUTO_RESEARCH 时才可能出现误触发。自动研究一次运行就要数分钟到数小时，路由多等几秒影响很小；而误触发会启动代码执行。原 LLM 路由在 v1 和 v2 上的误触发都是 0。

配置兼容：`config/decision_router.json` 中的 `tau_auto` 字段保留，但 cascade 不再读取。`validate_config` 不能因为它存在而报错。在 `config/decision_router.json` 旁边新增说明，或在 JSON 里加一个 `auto_research_policy: "llm_confirm"` 字段，`validate_config` 校验该字段只能取这一个值。

### 1.3 测试（`tests/test_decision_router.py`，使用 fake scorer 和 fake LLM，不下载模型）

1. scorer 判 AUTO_RESEARCH（概率 0.99）、LLM 返回 CHAT 时，最终结果为 CHAT，并且只调用了一次 `route_intent`，没有调用 `complete_route_fields`。
2. scorer 判 AUTO_RESEARCH、LLM 返回 AUTO_RESEARCH 时，结果与直接调用 `route_intent` 完全一致。
3. scorer 判其他意图时，现有各分支的测试结果不变。
4. `ROUTER_MODE=llm` 默认路径的请求序列不变（沿用现有断言）。
5. `evals/run_all.py` 离线验收通过。

### 1.4 离线复算（不调用模型）

用首次评测已保存的 E 组逐题记录，模拟新规则：E 组中小模型判为 AUTO_RESEARCH、且原本被接受的题，改为“需要 LLM 确认”。

- 统计 v2 和 v1 上各有多少题受影响，以及新规则下预计增加的远程请求次数。
- 只报告受影响的题数和远程请求增量，**不要**据此给出新的准确率，因为改判结果取决于 LLM 实际返回什么。
- 结果写进最终报告的“改动一”小节。

## 2. 改动二：4B 免训练打分（C1-4B）

1. 模型：Qwen3 系列 4B。先确认 Hugging Face 或 ModelScope 上的确切名称和许可证，优先 `Qwen/Qwen3-4B`；若有 `Qwen3.5-4B` 且许可证合适，可以改用，但写进变更记录。权重放在 `data/models/`，不进仓库。
2. 方法：与 0.6B 的 C1 完全相同，提示模板逐字不变（`prompt_sha256` 一致），使用 bf16，关闭 thinking 模式。不做 LoRA。
3. 在 dev_v2 上运行 forward、reverse、average 三种顺序，按原规则选择顺序，然后拟合温度和阈值，生成 `config/decision_router_c1_4b.json`。不覆盖现有的 `config/decision_router.json`。
4. 记录 4B 的显存峰值、冷启动时间、意图判定 p50/p95，与 0.6B 的 D 组对比。显卡为 RTX 4080 SUPER 16GB，bf16 下的 4B 应能放下；放不下时停止这一项并记录原因，不要改用量化。
5. 免训练的 4B 是否进入级联，按开发集结果决定：若它在 dev_v2 上的 macro-F1 高于 D，就准备一套 4B 级联配置；否则只作为对照组报告。不能看了 test_v3 再决定。

## 3. 新保留集 test_v3

### 3.1 规模与构成

在 `datasets/intent_routing/v3.json` 中只建测试集，不新增训练数据，共 160 题：

| 部分 | 题数 | 说明 |
| --- | --- | --- |
| 常规 | 七类各 15 题，共 105 | 覆盖与 v2 相同的场景标签 |
| 自动研究边界 | 35 | 无主题授权且历史无法解析（gold 为 CHAT）15 题；主题在历史中、可解析（gold 为 AUTO_RESEARCH）10 题；只调研/暂不实现/先别跑（gold 为 RESEARCH 或 CHAT）10 题 |
| 口语化 | 20 | 口语、无流程关键词的执行或联网请求，例如“帮我把那个 baseline 跑一下”，按实际意图标注 |

每题标注 `part` 字段，取值为 `regular`、`auto_boundary` 或 `colloquial`，报告时分层统计。

### 3.2 生成与复核

- 编写者使用一个新的代理会话，不能读取 v2 的 test/dev 内容、首次评测的错题和任何模型预测。它只能看到七类 `label_policy`、现有路由提示中的产品规则，以及场景标签清单。
- 由另一个代理在不看预测的情况下复核标签。沿用 v2 的记录字段（`reviewer`、`gold_labels_visible`、`model_predictions_read` 等），并写明不是人工标注。
- search_scope 需要在七类之间大致均匀分布，避免 v2 中 RESEARCH/DOWNLOAD 多带 web/arxiv 的偏差。
- 去重：与 train_v2、dev_v2、test_v2、v1 全集做精确去重、5-gram Jaccard（阈值 0.6）和 BGE-M3 余弦（阈值 0.92），命中的题从 v3 中删除并补写新题。阈值和删除记录写入 `dedup` 字段。
- 冻结后把 SHA-256 写入 `datasets/manifest.json` 和 `datasets/README.md`。冻结之前不允许任何组运行 v3。

可以扩展 `evals/build_intent_routing_v2.py` 以复用去重和冻结逻辑，也可以新建 `build_intent_routing_v3.py`。

### 3.3 运行组（每组只跑一次，失败计入分母）

| 组 | 说明 |
| --- | --- |
| A | 原 LLM 路由 |
| D | 0.6B LoRA，纯本地（decision 模式） |
| E2 | 0.6B LoRA 级联 + 改动一的 AUTO_RESEARCH 确认 |
| C1-4B | 4B 免训练打分，纯本地 |
| E2-4B | 仅在第 2 节第 5 条成立时运行 |

B、C0、旧 E 不在 v3 上重跑。

远程预算：每组 900 秒、200 次请求、100 万已报告 token。计分直接复用 `score()`，口径与首次评测一致。

### 3.4 报告指标（精简）

- 准确率和 macro-F1，并按 `part` 分层报告准确率。
- AUTO_RESEARCH 误触发数，单列 `auto_boundary` 部分中“无主题授权”那 15 题的处理结果。
- 非 CHAT 题被误判为 CHAT 的数量。
- 级联组的回退率，单独列出 `auto_research_confirm` 回退的次数。
- 时延：意图判定和完整路由的 p50/p95，分冷启动与热态（先预热 5 次）。
- 远程请求次数和已报告 token。
- D 和 C1-4B 的 ECE。
- 配对比较只做 E2 对 A：准确率差和 McNemar 检验，不做 bootstrap。

## 4. 报告格式

新建 `docs/research/decision-router-v3-results-YYYYMMDD.md` 和同名 `.json`。Markdown 要求：

- 全文中文，开头三行写清结论；
- 一张主表加一张时延成本表；
- 局限说明不超过 6 条，不再出现中英文重复；
- 逐题错误 ID 只写在 JSON 里，Markdown 中只列 AUTO_RESEARCH 误触发和 `auto_boundary` 的错题。

首次评测的报告和 JSON 保持原样，不修改。

## 5. 同步事项

- `scripts/export_source.py`：新增脚本加入 `CORE_EVALS`；若新增 v3 构建脚本，补充导出断言。
- `.env.example`：补充 4B 配置路径的说明（`DECISION_ROUTER_CONFIG` 可以指向 `config/decision_router_c1_4b.json`）。
- 默认 `ROUTER_MODE` 仍为 `llm`。是否切换默认值，等 v3 结果出来后由用户决定，本轮不改。

## 6. 里程碑

| 里程碑 | 交付 | 验收 |
| --- | --- | --- |
| F1 | 改动一 + 单测 + 第 1.4 节离线复算 | 第 1.3 节测试全部通过 |
| F2 | test_v3 构建、复核、去重、冻结 | manifest 中写入哈希；冻结前没有任何 v3 运行记录 |
| F3 | 4B 下载、dev_v2 顺序选择与校准 | 生成 `config/decision_router_c1_4b.json`，记录是否进入级联 |
| F4 | v3 上各组一次性运行 + 报告 | 报告满足第 3.4 节和第 4 节；导出副本离线验收通过 |

F1 和 F2 互不依赖，可以并行。F3 只依赖 dev_v2，也可以并行。F4 必须等 F1–F3 全部完成。

## 实施验收

- F1：AUTO 必须完整确认的回归检查通过；旧 E 组离线复算已进入新报告。路由实现检查点为 `6ef0bef3`。
- F2：160题经独立代理编写、另一代理逐题复核，精确、5-gram 和 BGE-M3 去重均无命中；数据、manifest 与 README 的冻结哈希一致。检查点为 `b8c40cae`。
- F3：4B 的三种顺序均在完整 dev_v2 上运行，选择逆序并校准；未达到加入级联条件。配置及评测源码检查点为 `f11fbb9`。
- F4：A、D、E2、C1-4B 各运行一次，完整结果与中文报告已提交为 `275aed1`。逐题指标、物理请求收据与预算已对账；旧 v2 数据和首次报告字节未变。测试计划及53份推理相关源码哈希一致。
- 源码导出到 `data/source-export-decision-router-v3-20261018`，456个文件与源码字节一致，包含4B配置、v3数据、构建器和完整报告，无环境凭据、Git历史及模型权重。在导出目录执行 `evals/run_all.py`，750项测试、7项跳过，全部离线阶段通过。日志保存在 `data/decision-router-v3-validation/export-offline.log`；实现后的最后变动仅为本文的验收记录。
- 实施前回滚入口为 `checkpoint/pre-decision-router-followup-20261018`（`730073f5`）。本轮提交均为本地保存。

## 变更记录

- 2026-10-18：逐题记录核对发现，第 1.1 节对 v1 误触发路径的推断不准确。该题先进入固定意图字段补全，但因预算字段不合法而回退完整 LLM 路由，最终 LLM 仍返回 AUTO_RESEARCH。v2/v1 分别有 7/1 题进入 AUTO 字段补全，且全部补全失败后回退；最终直接接受 AUTO 均为 0。第 1.4 节同时记录“门槛接受进入补全”和“最终接受”，预计少 7/1 次逻辑远程调用（忽略重试），不能离线推断准确率或保证误触发归零。仍按原方案实现统一 LLM 确认，等待独立 v3 验证。

- 2026-10-18：F1 已提交为 `6ef0bef3`，F2 独立编写、复核与去重后的160题已提交为 `b8c40cae`，F3 及正式评测代码已提交为 `f11fbb9`。v3 SHA-256 为 `a28b686618556f9fb6cb8bc51e970c729ad7e0e35cf0d5da697a558dfea86062`；对 v1/v2 全集及 v3 内部的三种去重均无命中。
- 2026-10-18：选用 `Qwen/Qwen3-4B@1cfa9a7208912126459214e8b04321603b3df60c`（Apache-2.0）。dev_v2 正序/逆序/概率平均 macro-F1 分别为66.12%/67.96%/66.11%，选择逆序并独立校准；低于 D 的94.03%，故不加入4B级联。正式计划在任何 v3 推理前冻结为 A、D、E2、C1-4B 四组，源码、配置和开发证据均绑定哈希。

- 2026-10-18：四组均完成首次正式运行，结果见[v3中文报告](decision-router-v3-results-20261018.md)及[完整JSON](decision-router-v3-results-20261018.json)。E2/A准确率为158/160、156/160，AUTO误触发均为0，15题无主题授权均返回CHAT；D与C1-4B在这15题上全部误触发。E2/A请求数98/168，均含预热。A有1次超时未返回token用量，报告明确标为不完整。配对McNemar p=0.6875，不能声称显著提升；默认仍为llm。未运行E2-4B，也未重跑旧test_v2或依v3调参。
