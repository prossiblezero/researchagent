# ResearchAgent｜证据驱动研究 Agent / Auto Research

**技术栈：** Python、SQLite、FTS5/BM25、BGE-M3、Chroma、OpenAI-compatible API、Codex CLI、JavaScript。

独立开发面向论文与技术研究的 Agent 工作台：普通聊天、资料问答和单次调研保持轻量；用户进入 Auto Research 后，系统按“证据→假设→实验→实现→测量→反馈迭代→报告”推进。

- **Auto Research Harness：**设计显式状态机和可恢复 Trace，编排原文检索、Claim–Evidence–Source 绑定、研究缺口与假设、baseline/ablation、宿主测量及结果决策；SQLite 管理报告状态、方案/实验版本和多会话研究区。
- **Agent-as-Tool 闭环：**将 Codex CLI 接入为受控编码工具，由宿主独立执行和评测。在 SciFact 官方 300 条开发集上跑通两版方案、6 组 measurement 和独立消融；candidate macro-F1 **0.4658→0.4739**，CONTRADICT F1 **0.0588→0.1770**。
- **证据与研究资产：**实现报告草稿/审核/发布、论文/方法/数据集/指标/结论关系、来源页码/章节/哈希/阅读窗口和引用核验；历史配对评测中原文片段覆盖 **91.67%→100%**，引用充分事实项 **19/22→20/22**。
- **RAG、Memory 与可靠性：**构建 FTS5/BM25＋BGE-M3＋RRF 混合检索和多文档重排，支持会话记忆、结构化压缩、检查点恢复、局部修复、沙箱与预算边界。SciFact Recall@5 **75.00%→87.50%**、MRR@5 **0.604→0.714**；LongMemEval Recall@5 **8.33%→37.47%**；Harness 端到端通过率 **31.25%→75.00%**。

项目配套开源基准与真实场景评测、运行轨迹、来源、指标和失败案例，可从 Web 工作台演示并复查。

[详细证据与面试说明](researchagent-20260930-r30-evidence.md)
