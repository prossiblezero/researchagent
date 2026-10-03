# 00｜项目地图：先找到一次问题会经过哪些文件

> 先分清入口、运行机制、数据和实验产物，再读函数细节。

[课程首页](README.md) · [下一章：跑通最小闭环](01-first-run.md)

## 问题：为什么按目录从上往下读很慢？

仓库同时保留了早期 CLI、当前 Web 工作台、实验执行器、评测脚本和历史报告。文件数量不能直接代表产品结构。尤其 `evals/reports/` 下还可能有隔离候选代码：那里用于记录某次实验，不一定是当前服务加载的实现。

最有效的读法是先锁定三条入口：

| 想理解什么 | 从哪里开始 | 下一跳 |
| --- | --- | --- |
| 一次研究如何完成 | [main.py](../../main.py) 的 `main()` | `ResearchAgent.run()` |
| 用户消息如何变成后台任务 | [server.py](../../server.py) 的 `make_server()`、`Handler.handle_api()` | `Workbench.send()`、`Workbench.execute()` |
| 调研如何扩展为编码实验 | [auto_research.py](../../research_agent/auto_research.py) 的 `AutoResearch.execute()` | `dispatch()` → `CodingTool.submit()` |

## 根目录：按职责阅读

下面是当前目录的语义地图，省略海量运行产物、缓存和环境内部文件；没有移动现有文件。

```text
researchagent/
├── main.py                         CLI 入口、模型/搜索依赖组装
├── stage1_agent.py                  旧单文件入口的兼容层
├── server.py                       HTTP API、静态页面、事件流
├── start-researchagent.ps1          本机现有部署启动入口
├── setup_retrieval.py               显式准备 BGE-M3 权重
├── research_agent/                 当前产品 Python 实现（39 个 .py）
├── web/                            原生 HTML / CSS / JavaScript 界面
│   ├── index.html                  木屋工作台页面
│   ├── app.js                      聊天、资料、任务、事件与上下文交互
│   ├── research-records.js          报告、关系、创新候选与实验面板
│   ├── strategies.js               策略反馈、候选与回退面板
│   └── style.css                   布局与视觉样式
├── tests/                          活动 Python 回归及前端测试
├── fixtures/                       确定性搜索/阅读示例
├── datasets/                       统一登记的评测输入与来源
│   ├── manifest.json               来源、用途、切分与哈希登记
│   ├── harness/、h1/               早期机制测试和评分校准
│   ├── retrieval/、evidence_qa/     检索与原文问答面板
│   └── open/、project/、research/、live/  开源子集及专项任务
├── evals/                          评测、复算、验收与报告生成脚本
│   ├── run_stage1.py               最小离线行为验收
│   ├── run_all.py                  全量单测与离线机制验收入口
│   ├── run_* / audit_*             各能力实测与结果审计
│   ├── reports/                    历史报告、原始轨迹、隔离候选
│   └── archives/                   历史归档
├── docs/                           设计、使用、验收与历史增量说明
│   ├── tutorial/                   本套教程
│   ├── resume/                     简历表述与数字依据
│   └── research/                   研究相关文档
├── data/                           本机运行数据、索引、备份等
├── traces/                         JSONL 运行轨迹
├── experiments/                    实验相关目录；活动任务路径看合同
├── .venv-v3/、.venv-eval-cuda/       本机环境，非产品源码
├── requirements.txt                PDF / 向量检索等依赖
├── requirements-retrieval.lock.txt 检索环境依赖锁定记录
├── .env.example                    配置模板
├── .env                            本机真实配置
├── Dockerfile、.dockerignore        容器入口及构建排除项
├── .github/workflows/ci.yml          Windows/Linux 离线验收
├── README.md                       项目总入口，保留历史信息
└── HANDOFF.md                      开发交接时间线
```

`data/` 和 `traces/` 的内容随运行改变；报告通常保存到数据库旁的 `reports/`。实验路径还受 `CodingTool.private_paths()` 与项目准备合同控制，不要只凭根目录 `experiments/` 推断某个任务的工作目录。

## 核心包：完整职责索引

### 1. 一次研究的内核

| 文件 | 职责 | 先找的符号 |
| --- | --- | --- |
| [contracts.py](../../research_agent/contracts.py) | 来源、证据、结论、工具响应与模型决策合同 | `Source`、`Evidence`、`Claim`、`ModelDecision`、`RunResult` |
| [loop.py](../../research_agent/loop.py) | 工具循环、预算、引用、核验与状态保存 | `ResearchAgent.run` |
| [models.py](../../research_agent/models.py) | 离线模型、HTTP 模型适配、选择和重试 | `OfflineModel`、`OpenAICompatibleModel`、`model_from_env` |
| [search.py](../../research_agent/search.py) | 搜索、网页读取、fixture 与公网访问检查 | `FixtureSearch`、`TavilySearch`、`HttpReader` |
| [policy.py](../../research_agent/policy.py) | 工具执行前和交付前的程序检查 | `before_tool`、`before_finalize` |
| [evidence.py](../../research_agent/evidence.py) | 来源去重、证据编号、引用格式与绑定 | `SourceCatalog`、`EvidenceCatalog` |
| [verify.py](../../research_agent/verify.py) | 确定性 Claim 诊断与在线段落核验 | `verify_claims`、`check_answer`、`apply_answer_patch` |
| [experience.py](../../research_agent/experience.py) | 本次运行的动作、观察、反馈记录 | `make_experience` |
| [context.py](../../research_agent/context.py) | 结构化输入裁剪与语义压缩 | `build_context`、`ContextCheckpoint` |
| [trace.py](../../research_agent/trace.py) | JSONL 事件、序号与敏感信息脱敏 | `TraceWriter`、`redact` |

### 2. 工作台与会话

| 文件 | 职责 | 先找的符号 |
| --- | --- | --- |
| [workbench.py](../../research_agent/workbench.py) | 意图分类、服务组装、任务分发与交付 | `route_intent`、`Workbench.send/execute` |
| [workbench_store.py](../../research_agent/workbench_store.py) | 研究区、会话、消息与 SQLite 队列 | `WorkbenchStore.enqueue/claim_next` |
| [storage.py](../../research_agent/storage.py) | 运行及来源、证据、结论等基础持久化 | `RunStore.save/get` |
| [sessions.py](../../research_agent/sessions.py) | 历史边界、分支、事件、检查点与恢复 | `Sessions.fork/prompt_context/save_state/resume` |
| [streaming.py](../../research_agent/streaming.py) | 模型流数据解析与累积 | 顺着 `models.py` 的导入进入 |
| [usage.py](../../research_agent/usage.py) | 请求用量汇总与未知用量统计 | `summarize_usage` |
| [reports.py](../../research_agent/reports.py) | 保存的 Markdown 报告转阅读页面 | `render_report` |
| [folders.py](../../research_agent/folders.py) | 本机文件夹选择及目录规则 | `choose_folder` |

### 3. 资料、检索与长期知识

| 文件 | 职责 | 先找的符号 |
| --- | --- | --- |
| [materials.py](../../research_agent/materials.py) | 下载、PDF/文本/仓库资料处理 | 从 `Library.ingest` 的调用进入 |
| [library.py](../../research_agent/library.py) | 资料目录、原文分块、分析与引用 | `Library.import_bytes/ingest/chunks`、`MaterialReader` |
| [retrieval.py](../../research_agent/retrieval.py) | FTS5、BGE-M3、Chroma、RRF 及回表校验 | `DenseIndex`、`Retriever.retrieve/read` |
| [evidence_rerank.py](../../research_agent/evidence_rerank.py) | 有界原文候选重排 | 由 `StrategyRetriever._rank` 接入 |
| [memory.py](../../research_agent/memory.py) | 记忆作用域、来源、确认、忘记与提取 | `Memory.visible/snapshot/extract` |
| [strategies.py](../../research_agent/strategies.py) | 反馈候选、配对评测、版本启用和回退 | `Strategies`、`StrategyRetriever` |
| [research_records.py](../../research_agent/research_records.py) | 报告版本、实体关系、假设和实验档案 | `ResearchRecords.capture/save_experiment` |

### 4. 自动研究与受控实验

| 文件 | 职责 |
| --- | --- |
| [auto_research.py](../../research_agent/auto_research.py) | 研究主控、工具调度、方案、比较与结束条件 |
| [coding_tool.py](../../research_agent/coding_tool.py) | 持久编码任务、代码收据、宿主模型接口和实测 |
| [research_project.py](../../research_agent/research_project.py) | 准备公开项目、数据和独立 Python 环境 |
| [experiments.py](../../research_agent/experiments.py) | 面板中的注册实验合同、提交、执行与恢复 |
| [experiment_process.py](../../research_agent/experiment_process.py) | Codex/实验进程、隔离、时限、取消与输出收集 |
| [experiment_acl.py](../../research_agent/experiment_acl.py) | Windows 实验目录权限的受控生命周期 |
| [experiment_iteration.py](../../research_agent/experiment_iteration.py) | 注册实验的轮次状态与停止策略 |
| [experiment_feedback.py](../../research_agent/experiment_feedback.py) | 实验结果和失败反馈整理 |
| [experiment_evidence.py](../../research_agent/experiment_evidence.py) | 实验原文证据相关辅助逻辑 |
| [experiment_dataset.py](../../research_agent/experiment_dataset.py) | 实验数据合同与准备 |
| [experiment_retrieval.py](../../research_agent/experiment_retrieval.py) | 注册检索实验实现 |
| [benchmark_datasets.py](../../research_agent/benchmark_datasets.py) | 评测数据集目录与材料解析 |
| [locomo_metrics.py](../../research_agent/locomo_metrics.py) | LoCoMo 答案、证据等指标计算 |
| [__init__.py](../../research_agent/__init__.py) | 包的公共导出接口 |

## 四种“版本”不要混淆

| 名称 | 在项目中指什么 |
| --- | --- |
| Stage 1–7 | 早期教学增量：循环、阅读、预算、验证、经验、策略钩子、Web |
| H1–H3 | 继承的 Harness 改善：可信评分、安全恢复、正文与上下文 |
| V1–V4 | 产品演进：研究区和任务 → 资料库 → 研究档案 → 受控实验/Auto Research |
| 报告/方案/实验版本 | 用户研究成果的数据版本，不等于软件发布版本 |

旧 Stage 4 的词法 Claim verifier 与当前在线段落核验不是同一能力。早期“H4 尚未开始”描述的是那条历史优化计划，不能据此说当前没有 `check_answer()`。V4 的注册实验流程和后加入的 Auto Research 也需要分别讲。

## 动手：画一张不看文档的地图

在纸上画出 `server → Workbench → ResearchAgent → model/tools → evidence/store`，然后加上 `AutoResearch → CodingTool → host measurement`。每个框写一个真实文件名。

查找函数时可直接运行：

```powershell
rg -n 'def (main|make_server|send|execute|run|dispatch|claim_next)' main.py server.py research_agent
```

完成标准：能在一分钟内定位“本地问答为什么不联网”“工具预算在哪里设”“失败从哪里恢复”三个问题的主要代码。

## 面试表达

“我把系统分为入口与会话、研究运行时、知识与证据、实验执行四部分。HTTP 只负责接收和展示；Workbench 负责路由及持久任务；ResearchAgent 负责有预算的检索阅读循环；AutoResearch 在其上管理研究方案、编码委派和宿主实测。数据主要在 SQLite，向量索引和 Trace 各有独立职责。”

自测：为什么不应该把 `evals/reports/` 里的候选实现当作线上源码？为什么只读 `main.py` 不能理解现在的完整产品？
