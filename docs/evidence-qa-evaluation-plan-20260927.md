# 原文重排完整问答验收（2026-09-27）

本轮先冻结 16 道题，再用同一个 Luna 配置、同一批完整公开来源和相同问答预算比较原有检索与原文证据重排。验收实际 `Workbench.execute` 的 `LOCAL_QA` 路径，包含会话上下文、检索、原文阅读、回答、引用核验、报告保存和记忆后处理；显式选择本地问答，因此不测意图路由准确率。

## 固定面板与边界

面板在 `datasets/evidence_qa/evidence_qa_cases_20260927.json`：普通问题、跨文档问题、干扰/错误前提、无答案各 4 题。22 个可回答事实逐一绑定完整来源片段、URL、页码及 SHA-256。无答案题记录具体资料范围、相关全文片段及缺失的信息类型；“未见于冻结资料”不等于“世界上不存在”。

来源沿用先前六个公开快照，事实包括 OCR 与版面输入分辨率的区别、ReAct API 返回语义与回退条件、Pi 摘要序列化、Codex 记忆字段和文件、OpenClaw MMR、Hermes 失败与压缩边界。这是新问答事实组合，不是新来源泛化，也不是公开榜单。后续独立来源面板须单独报告。

关键词匹配只提供复核预筛。最终由 Codex 逐项阅读答案和原文引用，以 `review.json` 保存 reviewer、时间、答案 hash、每事实 correctness/support/citation sufficiency、是否拒答、是否存在无依据新增事实及理由。这是 Codex 的逐条审阅，不是独立人类评分，也不是另一次 judge API。不能用关键词命中代替事实正确，更不能把原文字符串匹配当成语义蕴含。

## 配对条件

- 基线：原检索策略；候选：启用原文重排与阅读提示，其余开关关闭。两臂共用本轮一般性产品修复，不能把共同修复的收益归功于重排。
- 每个题目、每个处理臂都从纯来源 seed SQLite 备份出一个独立数据库和独立会话。报告、聊天与记忆不会进入后题或另一处理臂；向量索引也在各自目录重建。BGE 模型与相同来源向量仅使用既有进程内数值缓存。
- 奇数题先 baseline，偶数题先 candidate，交错执行；不并发争抢 API 与本机向量资源。
- quick 预算：8 次工具、12 轮、16,384 上下文 token；每题最多 32 次模型尝试、600 秒、300,000 已报告 token。检查发生在下一模型请求前，最后响应可能越过 token 门槛；缺报保持未知。
- 候选额外判断原文的请求和成本全部计入，仍遵守产品每任务最多两次新重排的限制；合法空结果与调用失败分别记录。

冻结文件包含实际模型名、温度、超时、端点 hash（不含 URL/key）、BGE 文件身份、代码、面板、来源、预算和两臂策略。QA 实际模块固定；不执行的 V4 实验模块另存整体仓库开始/结束清单，允许其独立开发。实验期间 QA 模块发生变化立即停止，不能混入同一条件。开发小样本输出与正式输出分开。

## 指标与归档

核心指标为Codex逐事实复核后的任务通过率、可回答题准确性、引用充分性、无答案正确拒答率、可回答题错误拒答率。产品 completed 状态与答案质量分别保存；端到端任务通过还需要产品成功交付。另报 gold 原文阅读/引用覆盖率、遗漏率、平均/P95 耗时、实际 provider 请求数、已报告 token 及计量覆盖率。固定 gold 只是已知充分片段，若模型引用另一确实充分片段，可在逐事实审阅中认可，但不事后修改固定 gold 指标。

gold 片段打开率只要求读到该片段的一部分，不能宣称通读。另报 `gold_fully_read_passage_rate`：实际 `read_evidence` 保存的可见文字必须覆盖该完整片段。原文重排读过的后台内容不冒充回答模型的显式阅读；事实是否已得到充分阅读仍按对应引用文字逐项检查。

每题保存实际请求与响应（脱敏但不显示截断）、所有请求 usage、完整 run/job、答案、事件、原文重排输入与返回、策略、逐事实复核和错误。常规 trace 可能为显示而截断，完整请求以 `requests/*.json` 为准。模型思考不作为事实依据或评分标签。

第一次尝试的失败不覆盖。恢复时跳过已完成任务；已留启动记录但未确认结束的任务不自动重发，以免重复收费。如果数据库已有终态则仅补导出；未知请求保存为中断。正式结果保留全部 32 个处理臂，不通过重跑挑选更好的结果。

## 操作

在 QA 代码与针对性测试稳定后：

```powershell
.venv-v3/Scripts/python.exe -X utf8 -B evals/run_evidence_qa_acceptance.py --stage freeze
.venv-v3/Scripts/python.exe -X utf8 -B evals/run_evidence_qa_acceptance.py --stage run
```

正式目录固定为 `evals/reports/evidence-qa-20260927/acceptance-r1`。调试时须用另一 `--output`，例如 `data/evidence-qa-20260927/pilot-r1`，并在 freeze 时用 `--case normal-01` 指定题目。run 根据冻结条件执行，重复运行不会重做已终结任务。

逐条完成 review 后运行修正口径的后处理器，生成 review-summary 下的指标、全部结果、失败案例、Markdown 和 HTML 报告：

```powershell
.venv-v3/Scripts/python.exe -X utf8 -B evals/postprocess_evidence_qa.py --output evals/reports/evidence-qa-20260927/acceptance-r1
.venv-v3/Scripts/python.exe -X utf8 -B evals/postprocess_evidence_qa.py --output evals/reports/evidence-qa-20260927/independent-r1
.venv-v3/Scripts/python.exe -X utf8 -B evals/build_iteration_report.py
```

首版 runner 的 `--stage report` 仍保留原始历史口径；它的整段阅读率把证据库全文当作实际输入，不能作为最终阅读指标。权威口径来自上述后处理器，仅计算成功回答/修复请求实际看到的原文窗口，并校验文字偏移。没有逐事实复核时报告显示待复核，不能先宣称通过。真实配对只允许由主任务统一启动，本面板作者不并行发起真实付费调用。

独立面板复用 `evals/run_independent_evidence_qa.py`，固定两份新的官方原文和16题；六份旧资料只作干扰。新运行必须指定新的 `--output`，不覆盖这两个已冻结的首轮目录。运行前冻结，运行后核对 end-manifest；`review-summary` 是不额外调用模型的复核汇总。
