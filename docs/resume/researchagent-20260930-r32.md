# ResearchAgent｜科研调研与自主研究 Agent

**技术栈：** Python、SQLite、BM25、BGE-M3、Chroma、Codex CLI。

独立开发研究工作台，支持论文调研、证据问答与按需启动的 Auto Research。

- **自主研究编排：** 自研研究控制器，将 Codex CLI 封装为编码工具，串联调研、方案、实现与实验反馈，支持基线/消融比较及结果驱动的方案迭代。
- **混合检索 RAG：** 融合 BM25、BGE-M3 与 RRF；SciFact 5,183 篇摘要、20 道保留题上，相对 BM25，Recall@5 **75.0%→87.5%**，MRR@5 **0.604→0.714**。
- **长任务记忆：** 实现分层记忆、历史检索与结构化上下文压缩；LongMemEval 25 道保留题上，历史会话 Recall@5 **8.33%→37.47%**。
- **Harness 可靠性：** 完善多工具调用、原文引用核验、局部修复与检查点恢复；固定 SciFact 8 题×2 配置复测，端到端通过率 **31.25%→75.0%**。

[指标证据与面试说明](researchagent-20260930-r30-evidence.md)
