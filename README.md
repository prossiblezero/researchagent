# 深度研究工作台 ResearchAgent

想快速读懂项目并准备面试，请从 **[Learn ResearchAgent 教程](docs/tutorial/README.md)** 开始：包含[当前文件结构](docs/tutorial/00-project-map.md)、12 章渐进式源码走读、离线练习和[面试讲解与演示](docs/tutorial/11-interview.md)。下方保留项目进展及历史阶段说明。

GitHub 发布范围见 **[发布说明](docs/github-publication.md)**：保留评测集、核心评测代码与测试，批量结果、归档和本地运行数据不上传；下文历史报告路径属于本地研究档案。



本仓库位于 `D:\project\researchagent`，面向计算机专业研究生实现研究区、后台调研、论文下载与理解、本地资料问答、创新方案及 Coding Agent 实验闭环。执行顺序见 [工作台路线](docs/deep-research-workbench-roadmap.md)；V1 已实现研究区、会话、意图路由与后台任务；V2 已实现资料下载、PDF 分析、GitHub 只读分析与本地证据问答，部署及真实联调状态见 [V2 验收](docs/v2-validation.md)；V3 已实现报告版本、证据与关系、创新候选和实验档案，见 [V3 使用说明](docs/v3-research-records.md) 和 [V3 验收](evals/reports/roadmap-v3-20260921/report.md)；V4 保留[有限实验迭代](docs/v4-controlled-experiments.md)，并新增[按需 Auto Research](docs/auto-research-v4-redesign-20260929.md)：研究 LLM 调用 Codex、宿主执行和评分、按反馈修订方案；已有有界科研闭环与独立终验记录，见[当前目标差距](docs/auto-research-goal-gap-20260929.md)。V5综合验收仍为后续阶段。使用方法和边界见 [V1 教学](docs/v1.md)，运行结果见 [V1 验收](docs/v1-validation.md)。



2026-09-14 从 `D:\project\newagent` 拆分，2026-09-16 通过复审并按文件同步 H1–H3。原仓库专注 Harness 和 Agent-RL；这里在继承的 Stage 1–7、SQLite 和简易 Web UI 上继续产品开发。同步范围与验证见 [H1–H3 同步记录](docs/h1-h3-sync.md)。现有 `.env` 保留；当前部署使用 `.venv-v3\Scripts\python.exe`，可运行 `start-researchagent.ps1` 启动木屋工作台。



## 当前进展（2026-10-03）

研究工作台支持普通聊天、单次调研、资料问答、头脑风暴和按需自动研究。用户明确要求自动调研、实现并按实验反馈迭代时，研究控制器才启动 Auto Research；研究区继续共享资料、Notes 和设置，区内会话独立，只有从历史消息分支才继承历史。

Auto Research 由研究 LLM 选择调研、读原文、保存假设/方案、委派 Codex、运行已有实验或结束。实验模型由宿主按预算调用，代码侧不接触凭据；宿主根据预测原件评分，将有效指标和失败案例返回研究模型，并保存方案、来源、代码、实验、报告及用量轨迹。实际源码、研究判断和结果都有可查收据，Codex 自报分数不等于宿主实测。

[开发评测](evals/reports/amem-corrected-measurement-audit-20261002/report.md)与[独立留出终验](evals/reports/amem-holdout-final-audit-20261003/report.md)均已完成三组测量和独立复算；MMR开发集有提升，留出集未获验证。原文核验失败、人工修正过的方法错误和各项指标的取舍均保留，尚不能宣称稳定的全自主科研、SOTA或论文质量目标达成。当前投递文案与数字口径见[简历入口](docs/resume/README.md)。 从[演示与证据入口](docs/auto-research-demo.md)查看普通调研、研究闭环和各类真实产物。

有限[Multi-Agent并行调研](docs/parallel-research.md)已实现并部署：主控可将两个独立问题交给各自拥有工具循环、证据和检查点的研究Agent，汇总后按需委派Codex或继续研究。共享SQLite，研究最多两路，编码/实验沿用原串行执行；[真实单/双worker对照](evals/reports/parallel-research-final-audit-20261003/report.md)已完成：16个子研究交付、严格合同6/8通过，两次约束转交遗漏保留；全部配对总体未提速。本次A-MEM收尾与适度并行交付到此结束，简历和完整证据见当前入口。

## 历史增量（2026-09-27）

普通问答新增可选原文证据重排、有限补查与证据不足交付；V4支持最多3轮编码，按累计预算、开发目标和连续无改善决定停止，保留全局最佳代码及每轮失败。现有V1–V3、共享资料/Notes/设置、多独立会话、历史分支和木屋界面保留。

[本轮真实问答与实验报告](evals/reports/evidence-qa-20260927/report.md) · [实施记录](docs/iteration-20260927-28.md) · [当前简历与数字依据](docs/resume/README.md) · [下一步](docs/next-iteration.md)。报告分别展示检索、实际阅读、引用、完整任务与模型用量，不把实验Recall直接当作答案准确率。已完成隔离回归358项后端、31项前端、V1 6/6及V2 5/5；真实面板状态以报告为准。

## 数据集与 benchmark 来源

所有活动评测输入统一位于 [`datasets/`](datasets/README.md)，活动测试代码统一位于 `tests/`。当前采用“开源原题子集＋项目真实场景”的 320 题面板：QASPER 80、SciFact 60、HotpotQA 50、LongMemEval 50、项目场景 80；实际产品检索和部分真实模型问答结果见 [产品评测](evals/reports/product-benchmark-20260929/report.md)。LongMemEval 当前报告历史会话召回，不是完整答案准确率；各子集结果均不是官方榜单成绩。A-MEM/LoCoMo 已完成开发集 40 题、788 轮记忆上的基线/候选/消融实测与[独立复算](evals/reports/amem-corrected-measurement-audit-20261002/report.md)，指标有改善也有退化；另 40 题、1292 轮的[独立留出终验](evals/reports/amem-holdout-final-audit-20261003/report.md)已完成，候选未达到冻结收益条件，完整指标、费用和失败均已归档。AgentBench、τ-bench、SWE-bench 仍仅作参考。来源、用途、切分、许可证和哈希见 [`datasets/manifest.json`](datasets/manifest.json)。

## Agent 正式评测

新增可验证的跨任务策略改进：失败反馈、有限检索/阅读候选、固定配对重放、模型限定启用、运行归因与回退。入口为木屋侧栏「策略改进与反馈」，使用与边界见 [策略说明](docs/harness-strategies.md)，本轮真实结果与失败见 [Harness 策略对照](evals/reports/harness-strategies-20260921/report.md)。沿用 SQLite、既有检索/记忆/会话基础；策略模块不训练模型，受控代码执行由独立的 V4 实验合同授权。

2026-09-18 建立并执行 72 次冻结开发评测，覆盖 pass@1/2/3、pass^1/2/3、步骤与 token、同义改写、引用文本攻击、上下文长度及证据位置。真实模型 Luna，语义评分 Terra；流程指标和内容正确性分开，失败和缺失用量均保留。方法与复现见 [Agent 评测](docs/agent-benchmark.md)，本轮结果见 [正式评测报告](evals/reports/agent-formal-20260918/final-report.md)。这是自编开发任务的可复现基线，不是公开榜单或独立保留集成绩。

## V2 真实验收

2026-09-18 使用 Luna 执行自编真实任务，最终 **14/15（93.3%）**；本地摘录匹配 26/26、下载完整性 3/3。剩余一项开放式研究因缺少有效引用失败。全部指标、失败证据和截图见 [验收报告](evals/reports/v2-acceptance-20260918/report.md)，不代表公开 benchmark 成绩或通用答案正确率。

复现：`python -B evals/run_v2_benchmark.py --download-root D:\paper`。每次新建测试研究区并保留独立结果；使用现有真实模型配置。

## Harness 优化状态



V1 现在由模型自动选择论文索引、会议官网、预印本或技术文档等来源，并按问题复杂度选择研究预算；无需手选 arXiv。界面采用木屋书房主题，保留大字体、炉火动画/环境音、实时 Trace 和本机文件夹选择。V2 新增独立资料库；使用方法见 [V2 教学](docs/v2.md)。



继承的 H1–H3 已同步，V1 已实际开发并完成工程验收。`newagent` 仍为 `PAUSED_FOR_RESEARCHAGENT`；本次只修改 researchagent，未自动恢复 H4。



| 阶段 | 冻结基线 -> 最终结果 | 结论 |

|---|---|---|

| H1 可信评分 | 校准 `0/4 -> 4/4`，H1 测试 `14/14` | 判分器严格改善 |

| H2 安全与恢复 | 共享探针 `1/7 -> 7/7`，强化门禁 `14/14` | 严格改善、无旧通过项回归 |

| H3 正文与上下文 | 同版旧冻结基线行 `6/10 -> 10/10`，断言 `92/118 -> 118/118`，质量守卫 `20/25 -> 25/25` | 严格门禁通过；本次收尾前为 `9/10`、`115/118` |



本轮报告分别是 `evals/reports/h1-calibration-post-h3-p2-v7.json`、

`h2-post-h3-p2-v7.json` 和 `h3-p2-after-v7.json`。隔离副本中全量单元测试 `88/88`、全部离线检查通过。

H3 三份 v7 报告的 runner、H3 测试、数据和配置一致；模型输入成本与本次收尾前相同，

低于历史 pre-H3 上界。复现和哈希见 [评测文档](docs/evaluation.md) 与 [同步记录](docs/h1-h3-sync.md)。

这些是确定性 Harness 行为/成本证据，不是通用模型质量分数。真实 dev C 的 150 次运行因 Windows

`WinError 10013` 全部工程失败，真实 A–D 质量对照尚无可用结论。



这是一个按 `HANDOFF.md` 增量实现的透明研究 Agent。Stage 1–7 的可运行版本已完成，核心机制是：



```text

模型决定动作 → 调用 search/read 工具 → 记录 Evidence → 模型生成带引用回答

```



控制流由普通 Python 显式编排，不使用 LangGraph 或其他 Agent 框架。每次运行都会把状态转移写入 JSONL Trace，便于回放和评测。



## 目录



```text

researchagent/

├── main.py                  # CLI，只有参数解析和依赖组装

├── research_agent/

│   ├── contracts.py         # Source / SearchResponse / ModelDecision

│   ├── search.py            # fixture、失败 fixture、Tavily/HTTP JSON 搜索

│   ├── models.py            # 离线模型、OpenAI-compatible 客户端

│   ├── trace.py             # JSONL Trace 和凭据脱敏

│   ├── evidence.py          # 来源归一化和引用校验

│   ├── context.py           # H3 结构化上下文和输入预算

│   ├── policy.py            # H2 工具前/输出前策略

│   └── loop.py              # 唯一的显式 Agent Loop

├── fixtures/search_results.json

├── datasets/harness/stage1_cases.json

├── evals/run_stage1.py

├── datasets/harness/evidence-agent-offline-v1.json # 声明式离线 Harness 任务集

├── evals/run_metrics.py       # 量化指标与 JSON 报告

├── evals/run_h1_calibration.py # H1 判分校准门禁

├── evals/run_h2_eval.py        # H2 安全与恢复门禁

├── evals/run_h3_eval.py        # H3 正文与上下文门禁

├── evals/latest_metrics.json  # 最近一次离线基线

├── tests/test_stage1.py

├── tests/test_evaluation.py   # H1 评分回归

├── tests/test_h2.py           # H2 安全与恢复回归

├── tests/test_h3.py           # H3 正文与上下文回归

├── docs/stage1.md

├── docs/stage2.md … docs/stage7.md

├── server.py

├── research_agent/storage.py # SQLite runs/sources/evidence/claims 持久化

├── data/evidence_agent.db   # 运行时生成（已忽略）

└── Dockerfile

```



模块按变化原因拆分：协议、搜索、模型、Trace、证据整理和编排分别可替换；没有为未来功能预留空抽象。



## 已实现



- 单一 `search` 工具和最大调用次数预算（默认 2）。

- 标准库实现的 OpenAI-compatible Chat Completions 适配器（从环境变量读取配置）。

- Tavily 搜索适配器；配置 `TAVILY_API_KEY` 后自动启用真实搜索。

- 无 API key/网络时的确定性 fixture 搜索和离线模型，保证示例可运行。

- 结构化搜索结果、每次运行生成 `S1`、`S2` 来源 ID，以及最终引用校验。

- 工具失败、模型失败、非法动作和证据不足时安全停止，不编造来源或结论。

- 公网地址、DNS、重定向、凭据 URL 和媒体类型检查；参数纠正、瞬态重试及 final-only 总结。

- HTML 正文抽取、正文/摘要分离、内容哈希/截断元数据和兼容 SQLite 迁移。

- 输入/输出预算分离；保留原问题、S/E 映射和最近工具对，保护内容超预算时明确失败。

- Trace 不记录 API key；模型 prompt 仅记录哈希，不记录思维链。

- SQLite 保存每次研究的运行记录、来源、Evidence、Claims、Experience 和审计事件。

- 内置浏览器 UI，可提交问题、查看历史任务、答案、来源、Claim-Evidence 和 Trace。



## 快速运行（离线）



推荐使用刚创建的独立 conda 环境；不要直接调用 `base`：



```powershell

$CONDA = 'E:\anaconda\Scripts\conda.exe'

$envName = 'evidence-agent'

# 只需首次执行；不会修改 base

& $CONDA create --name $envName python=3.12 -y

# 后续每条命令都显式指定独立环境，不调用 base

& $CONDA run --no-capture-output -n $envName python main.py

& $CONDA run --no-capture-output -n $envName python main.py --fail-search '请核验一个事实'

& $CONDA run --no-capture-output -n $envName python -m unittest discover -s tests -v

```



输出会列出答案、来源和 Trace 路径（默认 `traces/*.jsonl`）。



## 配置模型（OpenAI / DeepSeek）



从项目根目录的 `.env.example` 创建 `.env`，再填入密钥。程序会自动切换到 HTTP 适配器；未完整设置时仍使用离线模型。

`.env` 已加入 `.gitignore`，不要提交真实密钥：



```powershell

# DeepSeek

PROVIDER=deepseek

BASE_URL=https://api.deepseek.com/v1

API_KEY=你的-deepseek-key

MODEL=deepseek-chat



# OpenAI（四行替换上面的配置）

# PROVIDER=openai

# BASE_URL=https://api.openai.com/v1

# API_KEY=你的-openai-key

# MODEL=gpt-4o-mini

```



也可以在 PowerShell 中临时设置 `$env:BASE_URL`、`$env:API_KEY`、`$env:MODEL`；

环境变量优先于 `.env`。程序使用标准库读取配置，不需要安装 `python-dotenv`。



搜索优先级为：`SEARCH_URL` 自定义 JSON 服务 > `TAVILY_API_KEY` Tavily > 本地 fixture。

Tavily 配置写在 `.env`：



```dotenv

TAVILY_API_KEY=你的-tavily-key

# TAVILY_URL=https://api.tavily.com/search

```



Tavily 返回的 `title`、`url`、`content` 会映射为项目内部的来源字段。API key 只放在请求体中，

不会写入 Trace。若要使用自定义搜索服务，设置 `SEARCH_URL`（返回 `{ "results": [...] }`）和可选 `SEARCH_API_KEY`。



配置完成后可做一次真实联调（会消耗模型/搜索额度）：



```powershell

conda activate evidence-agent

python main.py "请查找 Tavily 的官方文档，并给出来源。"

```



详细教学请看 [docs/stage1.md](docs/stage1.md) 和 [docs/stage2.md](docs/stage2.md)；评测指标解释见 [docs/evaluation.md](docs/evaluation.md)，评测设计依据见 [docs/evaluation_research.md](docs/evaluation_research.md)。



启动交互式 Web UI：



```powershell

conda activate evidence-agent

python -m pip install -r requirements.txt

$env:OFFLINE_MODE='1'; python server.py

```



打开 <http://127.0.0.1:8000>，创建研究区后开始聊天或后台研究。默认使用 `data/evidence_agent.db` 保存历史和任务，报告保存到数据库旁的 `reports/`；可用 `DB_PATH` 指定其他 SQLite 文件。



真实模式先移除 `OFFLINE_MODE` 再启动，沿用现有 `.env`；缺少模型/搜索配置会明确报错。详细 API、恢复规则和 Docker 命令见 [V1 文档](docs/v1.md)。



`stage1_agent.py` 只是旧单文件版本的兼容入口；正式入口是 `main.py`。



## 阶段 1–7 的实现边界



- Stage 1：search + 显式 Agent Loop。

- Stage 2：read、Evidence、Claim。

- Stage 3：多轮预算、重复动作停止、经验记录。

- Stage 4：独立 Claim 验证和 `SUPPORTED`/`INSUFFICIENT`/`CONFLICTING` 状态。

- Stage 5：任务内 Chain-of-Experience 记录。

- Stage 6：Policy Hook、Audit 事件和离线评测入口。

- Stage 7：标准库 HTTP 服务和 Dockerfile。



这些是 H1 前已有的可运行最小版本。旧 Stage 4 的 lexical verifier 不等于优化计划 H4 的语义

验证/补查闭环；后者尚未开始。真实来源冲突判定、复杂多 Agent 编排和生产级部署仍需基于评测结果继续增强。



## Trace 事件



Trace 至少包含 `run_started`、`model_request`、`model_response`、`tool_call_requested`、`tool_result`、`final_validated` 和 `run_finished`。外部结果在消息中以 `UNTRUSTED_TOOL_DATA` 标记；网页中的指令性文字只当数据处理。



## Stage 1 评测集



有。`datasets/harness/stage1_cases.json` 是一个小型、确定性的行为评测集，当前覆盖：



- 普通问候不调用工具

- 成功搜索并绑定 `[S1]`

- 搜索失败后返回 `INSUFFICIENT`

- Prompt Injection 内容只当数据



运行：



```powershell

conda activate evidence-agent

python evals/run_stage1.py

```



它输出每条样例的 PASS/FAIL 和总通过数。这个评测集检查的是 Stage 1 控制流与安全边界，

不是衡量真实大模型知识质量；后续阶段再加入 Claim 支持率、引用准确率和多跳研究指标。



## 量化评测指标



`evals/run_metrics.py` 在固定 fixture 上计算可复现的 Harness 回归指标：任务通过率、部分完成分、引用准确率、Evidence 覆盖率、平均工具调用/轮次、工具成功率、Pass@k、Policy 拒绝率、安全失败率和重复运行稳定性。它不使用 H1 人工标注协议，也不能替代真实 API 质量评测。字段含义见 [docs/evaluation.md](docs/evaluation.md)。



```powershell

conda activate evidence-agent

python evals/run_metrics.py

```



当前 7 条离线用例基线：任务通过率 `1.0`、引用 ID 有效率 `1.0`、lexical Claim

支持诊断 `0.667`、平均工具调用 `0.857`、平均轮次 `1.857`、安全失败率 `1.0`、

重复运行稳定性 `1.0`。lexical 值不是 H4 语义支持率；整组数据只是 fixture 行为基线，

不能外推到真实 API。



`evals/live_report.json` 是旧判分规则下的历史工件，其中的 8/15 和引用率不能作为当前质量结论。当前 H1 入口、版本化标注协议和复现命令见 [`docs/live-evaluation.md`](docs/live-evaluation.md)。



运行全部离线检查：



```powershell

python evals/run_all.py

```



## 面试话术（阶段 1）



“我先用显式状态实现最小闭环，让模型只能在唯一搜索工具和最终回答之间选择；工具结果原样回灌并带不可信标记，Trace 记录每次状态转移和引用校验。这样先证明可观察、可失败、可评测，再逐阶段增加 Claim 验证和多跳研究。”



## 检索、上下文与记忆优化（2026-09-18）

A/B1/B2/C/D 已集成到 V2：BGE-M3 + Chroma/FTS5 工具检索、结构化压缩、会话检查点/分支、带来源的研究区记忆，以及请求级 token/缓存观测。当前使用独立 `.venv-v3` 环境；[使用与边界](docs/context-memory.md)。[评测与真实案例](evals/reports/context-memory-optimization-20260918/report.html) 保存最终指标、原答、来源、失败及复现记录。后续 V3 已增加有原文依据的创新候选与实验版本档案，见 [V3 文档](docs/v3-research-records.md)；V4 已接通按需 Auto Research、Codex 工具委派与宿主实验反馈；完整科研验收进度见[当前目标差距](docs/auto-research-goal-gap-20260929.md)。
