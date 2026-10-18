# 决策模型式意图路由

默认 `ROUTER_MODE=llm`，仍调用原 `route_intent`。`cascade` 使用本地七选项打分和 dev_v2 校准阈值；模型、adapter、提示构造哈希不匹配，低置信或任何推理/字段错误均回退原路由。`decision` 仅供评测，跳过置信门槛。

本实现参考决策模型的选项打分方法，不使用 Jev API，也不声称对比 Jev 本身。

## 环境与权重

训练依赖在 `requirements-router.txt`，不加入最小运行依赖。当前机器复用 `.venv-v3/Scripts/python.exe` 和 `.venv-eval-cuda` 的 CUDA torch 覆盖层；新增包位于 `.venv-router`。在仓库目录的 PowerShell 中：

```powershell
$env:PYTHONPATH="$PWD\.venv-router;$PWD\.venv-eval-cuda"
.venv-v3/Scripts/python.exe -X utf8 evals/run_decision_router_eval.py --hardware
```

模型是 `Qwen/Qwen3-0.6B`，revision `c1899de289a04d12100db370d81485cdf75e47ca`，Apache-2.0。权重放 `data/models/Qwen3-0.6B`，不提交 Git。`OptionScorer` 仅从本地加载，不在生产请求中下载模型。CPU 用 fp32，CUDA 用 bf16。

在普通隔离环境安装 `requirements-router.txt` 后，可按固定版本准备模型：

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-0.6B', revision='c1899de289a04d12100db370d81485cdf75e47ca', local_dir='data/models/Qwen3-0.6B')"
```

七个后缀 `1]` 至 `7]` 的实际 token 为 `[16,60]` 至 `[22,60]`；完整累加数字和右括号的 log-prob。prompt prefill 一次，后续七候选共享/扩展 KV cache。正序、逆序和按意图对齐后的概率平均仅在 dev 选择。

## 数据与实验纪律

`datasets/intent_routing/v2.json` 包含 1,400 条训练、70 条开发和140条保留测试。训练与开发/测试由不同代理编写，再由第三个代理逐条复核；不是人工标注。数据内保存来源、复核、MASSIVE 排除/替换、修订和去重记录。模板及主题相关性仍然存在，不能把置信区间当作真实生产分布保证。

构建器接收独立作者的 JSON，使用规范化精确比较、字符 5-gram Jaccard 0.6 与本地 BGE-M3 余弦 0.92 检查 train 相对 dev/test/v1 的重叠；冻结时还检查复核后的最终文本。哈希登记在 `datasets/manifest.json`。已经使用过的 v1 test 只作回归。

```powershell
python evals/build_intent_routing_v2.py assemble --train <train.json> --heldout <heldout.json> --sources <MASSIVE源目录> --out <待复核.json>
python evals/build_intent_routing_v2.py freeze --reviewed <独立复核.json>
```

已有冻结文件和输出目录不能覆盖。下面的开发命令可使用新的输出目录；本项目已冻结数据，不应重复执行 freeze。

```powershell
python evals/run_decision_router_eval.py --group C1 --split dev_v2 --order forward --out evals/reports/<新目录>
python evals/calibrate_decision_router.py --run <完整C1或D开发运行> --out <新校准目录>
python evals/train_decision_router.py --baseline-run <选定C1开发运行> --out data/models/<新训练目录>
```

LoRA 主目标是与推理相同的完整候选 listwise CE；KL 对齐冻结 base 的最后四个非答案 prompt 位置，每16步增加一条仅做 KL 的通用中文指令。默认 rank16、q/k/v/o、一个 epoch、seed17，开发集比较 `5e-5/1e-4/2e-4`；每四分之一 epoch 检查 macro-F1，并要求 ECE 不超过未校准 C1 的 ECE+0.03。所有检查点指标与 loss 曲线保留。

正式 `test_v2` 命令要求 `--plan` 指向开发选择后冻结的 JSON，其中 `panel_sha256` 与数据一致，`groups` 按 A/B/C0/C1/D/E 保存 `evaluation_identity(args, config)` 的结果，包含顺序、字符上限、模型/编码器权重、adapter、提示和配置哈希，以及远程别名、实际模型名和端点主机名（无凭据）。提供校准配置时，CLI 顺序和字符上限必须一致。验证在加载模型和占用名额之前完成。

每组第一次启动会在 `data/decision-router-holdout/<数据哈希>/` 原子登记，并保存整份计划及其哈希；后续组及汇总报告必须匹配同一计划。中断或失败也消耗首次运行，不删除登记来挑最好成绩。

```powershell
python evals/run_decision_router_eval.py --group E --split test_v2 --plan <冻结方案.json> --config config/decision_router.json --adapter <选定adapter目录> --order <开发选择> --out <新正式目录>
```

A 是原 LLM 路由，B 是 BGE-M3+逻辑回归，使用编码器原生 token 上限；超过时先丢弃旧历史，最新消息不能完整容纳则失败。C0 是同一小模型贪心生成标签，C1 是免训练选项打分，D 是 LoRA 打分，E 是选定本地方法加 LLM 回退。A/E 共用既有 Budget 记账机制，每组上限900秒、200次物理请求、100万已报告 token；未知用量按预留额记账。所有失败保留在分母。评测只分类和生成路由字段，不执行被路由的任务。

B/C0/C1/D 仅报告意图确定时延；A/E 才报告完整路由时延。每次本地运行单列加载冷启动，B 另记训练向量编码和分类器拟合时间。预热五次后计热态；A/E 的五次远程预热使用同一预算并保留成本收据。v1 回归 E 使用原 v1 test 的 1200秒/280请求/150万token上限，A复用历史响应。必须表述为“本地 GPU 小模型 vs 远程 API”，不能声称同硬件条件的模型加速。

## 接入与验证

将选定校准目录的 `config.json` 保存为 `config/decision_router.json`；部署模型和 adapter 后才启用 `ROUTER_MODE=cascade`。可用 `DECISION_ROUTER_MODEL`、`DECISION_ROUTER_ADAPTER`、`DECISION_ROUTER_DEVICE`、`DECISION_ROUTER_CONFIG` 指定本地路径/设备；默认配置与开关仍保留原 LLM 行为。

同步请求和后台 `pending_route` 都通过 `select_router`。新增 `intent_decision` 记录概率、温度、置信度、margin、回退原因、意图确定/路由完成耗时和字段补全调用。原 `intent_routed` 字段不变。四类无需字段的意图零 LLM 调用；CHAT/RESEARCH 的补全不能改变意图，输出复用 `_parse_route` 校验和脱敏。自 2026-10-18 起，cascade 中本地 top-1 为 AUTO_RESEARCH 时一律交给完整 `route_intent` 确认，允许改判为 CHAT 澄清；记录 `fallback_reason=auto_research_confirm`。`tau_auto` 仅为历史配置兼容保留，cascade 不再使用；`auto_research_policy` 缺省或显式取 `llm_confirm`，其他值拒绝加载。decision 评测路径保持原样。

```powershell
python -m unittest discover -s tests -p test_decision_router.py
python evals/run_all.py
node --test tests/test_workbench_frontend.cjs
python scripts/export_source.py data/<新导出目录>
```

CI 不下载模型。数值测试使用 fake logits/cache；真实 tokenizer 切分收据、冻结哈希、默认请求序列与失败回退都有离线检查。源码导出包含配置、依赖清单、所有新评测脚本及数据，不包含模型权重和实验运行目录。

## 本轮冻结配置

当前配置来自 seed17、学习率 `2e-4`、第350步的 LoRA，选项顺序为概率平均；全部三个学习率和12个开发检查点均保留。温度及阈值见 `config/decision_router.json`，选择只使用 dev_v2。该配置引用的 adapter 保存在本机 `data/models/decision-router-20261017/lr-0.0002/step-350/adapter`，不进入 Git 或源码导出。

首次140题保留测试中，A与E准确率均为136/140，AUTO_RESEARCH误触发均为0。E使用83次远程请求、164,023个已报告token，A为148次、419,837个（均含5次预热）。这些是单次本地GPU与远程API部署比较；配对统计、完整失败及局限见[实验报告](decision-router-results-20261018.md)。默认仍为原LLM路由。

冷启动表示本进程首次模型加载；B的显存峰值包含训练向量编码，拟合时间另列。评测协议中的 `temperature` 表示本地校准温度，不是远程服务的采样温度。

v1回归中级联有1/184个AUTO_RESEARCH误触发，原路由为0/184；虽然v2为0/120，仍不足以支持默认切换。阈值保持开发集冻结结果，未用测试或回归失败调参。


## 2026-10-18 后续实验

`cascade` 的 AUTO_RESEARCH 现在必须经完整 LLM 路由确认；配置中的 `tau_auto` 为历史兼容字段，不再用于级联。旧 v2 成绩与报告保留原样。v3 是只含160题的独立保留集，按 regular / auto_boundary / colloquial 分层；冻结前不准运行分类器，各组第一轮即为正式结果。

4B 对照使用 `Qwen/Qwen3-4B`，revision `1cfa9a7208912126459214e8b04321603b3df60c`，Apache-2.0，bf16、不量化、关闭thinking。提示模板与0.6B相同。dev_v2 的正序/逆序/平均 macro-F1 分别为66.12%/67.96%/66.11%，据原规则选择逆序；独立配置为 `config/decision_router_c1_4b.json`。其开发集表现低于0.6B LoRA的94.03%，因此不运行E2-4B。此决定在任何v3预测之前冻结。

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-4B', revision='1cfa9a7208912126459214e8b04321603b3df60c', local_dir='data/models/Qwen3-4B')"
python evals/build_intent_routing_v3.py audit --candidate <独立复核.json> --out <去重收据.json>
python evals/build_intent_routing_v3.py freeze --reviewed <独立复核.json> --audit <去重收据.json>
python evals/report_decision_router_v3.py --select-four-b <包含三种4B开发运行的目录> --out <新4B配置.json>
python evals/report_decision_router_v3.py --prepare-plan <新研究目录> --out <新研究目录>/test-plan.json
python evals/run_decision_router_eval.py --group E2 --split test_v3 --plan <冻结计划.json> --config config/decision_router.json --adapter <冻结adapter目录> --order average --out <新研究目录>/test-E2
python evals/report_decision_router_v3.py --study <已完成研究目录> --out docs/research/<新报告前缀>
```

v3 允许 A、D、E2、C1-4B，以及仅在4B开发集macro-F1严格高于D时允许的E2-4B。正式计划绑定推理源码、模型与配置身份；分组名、源码变化或重复运行均拒绝。远程预热也经过该组真实路由并计入预算。首次本地预热、首次远程预热与加载时间分列；只有5次预热之后的逐题记录计算热态p50/p95。本地D与C1-4B只判意图，不调用LLM补字段。配对比较仅做E2−A的准确率差与McNemar精确检验。

数据构建的独立作者只读标签政策、产品规则和场景标签；复核代理不看旧题或预测。复核收据绑定最终cases字节，去重收据绑定整个复核文件和v1/v2引用哈希；修改任何题面、标签、scope或annotation之后须重新复核及去重再冻结。

v3 四组首次结果已固定，见[中文报告](decision-router-v3-results-20261018.md)与[完整逐题 JSON](decision-router-v3-results-20261018.json)。E2 准确率158/160（98.75%），A为156/160（97.50%）；两组AUTO误触发均为0，15题无主题授权均返回CHAT。D为134/160，4B免训练为103/160；D与4B在15题无主题授权上均判为AUTO，因此纯本地decision仍只适合评测。E2回退率37.50%，其中40次为完整AUTO确认。

E2与A分别使用98/168次远程请求（均含预热），减少41.67%；完整路由热态p95为19.066/27.002秒。E2已报告218,773 token；A已报告488,182 token，但另有一次超时未返回用量，故其总token不完整，不将两者换算成精确成本节省。配对McNemar p=0.6875，不能据此声称显著优于或等效于A。保持默认ROUTER_MODE=llm，不用v3重新选择模型、顺序或阈值。
