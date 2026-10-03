# ResearchAgent｜证据驱动研究 Agent 与按需 Auto Research

**技术栈：** Python、SQLite、FTS5/BM25、BGE-M3、Chroma、OpenAI-compatible API、Codex CLI、JavaScript。

独立开发面向论文与技术研究的 Agent 工作台。普通聊天、论文调研和资料问答保持轻量；用户明确进入 Auto Research 后，系统沿“原文证据→研究假设→方案实验→Codex 实现→宿主测量→反馈迭代→可追溯报告”推进。

- **Auto Research 控制器：**以显式状态机编排检索、原文阅读、Claim–Evidence–Source 绑定、方案版本、baseline/ablation、Codex 委派和宿主执行；研究 LLM 根据真实 measurement 选择继续调研、修订方法、重跑或结束。SQLite 保存检查点、Trace、报告和实验版本。
- **Agent-as-Tool 与真实反馈闭环：**将 Codex CLI 接入为可控编码工具，宿主独立执行和测量。在 SciFact 300 条开发集上跑通两版方案、3 次 Codex 委派、2 次纯测量和 6 组有效 measurement；跨轮仅修改 `algorithms.py`，评分器与数据保持冻结。candidate macro-F1 **0.4658→0.4739**，CONTRADICT F1 **0.0588→0.1770**，并完成独立消融。
- **证据与研究资产管理：**支持报告草稿/审核/发布、论文/项目/方法/数据集/指标/结论关系、HYPOTHESIS 创新候选、来源页码/章节/哈希/阅读窗口和引用核验；历史配对评测中，原文片段覆盖 **91.67%→100%**，引用充分事实项 **19/22→20/22**。
- **RAG 与 Memory：**构建 FTS5/BM25＋BGE-M3＋RRF 混合检索和多文档重排。SciFact Recall@5 **75.00%→87.50%**、MRR@5 **0.604→0.714**；LongMemEval 历史会话 Recall@5 **8.33%→37.47%**；真实模型记忆专测 **20/20**。
- **可靠性与评测：**实现多工具协议处理、引用核验、局部修复、检查点恢复、沙箱和预算边界；组织 **320 条开源基准＋真实场景任务**，保存数据版本、运行轨迹、指标和失败案例；主回归 **454 passed, 1 skipped**。

**当前状态：**已具备可运行的研究工作台、证据 Harness 和 Auto Research 基础闭环；复杂课题的多数据集、多 seed 科学复验和长期稳定的自主继续/停止决策仍在迭代。

[证据与面试说明](researchagent-20260930-r29-evidence.md)
