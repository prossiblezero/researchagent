# r14 简历证据与面试说明

2026-09-30（北京时间）。[当前简历](researchagent-20260930-r14.md)；保留 [r13](researchagent-20260929-r13.md) 及其历史证据。本版增加一次真实方法修订与重跑案例，不改旧评测分数，不用外部 Codex 的编码能力冒充自研算法收益。

## 项目贡献与最新验收

研究 Agent 负责目标、证据、方案、工具选择及反馈决策；Codex 是实现工具；宿主执行器负责独立测量和可追溯收据。研究控制器已有这些能力，尚未证明可以稳定完成复杂自主科研。

SciFact iteration-r2：300 条官方 dev claim，给定 cited_doc_ids 的三分类子任务；2 版方案、每版 baseline/candidate/ablation 共 6 组结果。研究 LLM 发现 candidate 的 CONTRADICT F1=0，结合 RerrFact 原文提出先 NOINFO、后 SUPPORT/CONTRADICT 的两阶段方法，交给 Codex 实现并重跑。固定评分入口及基线不变，六组逐样本评分独立重算一致，首轮代码另在原生沙箱独立重跑 3/3 一致；没有新增模型费用。

候选 accuracy 0.5367→0.5800、macro-F1 0.4224→0.4388，但仍低于基线 0.6067/0.4831，最弱类别 F1 未改善。因此正文描述研究行为及验收规模，不把这几个分类分数作为研究 Agent 独立贡献或有效创新证据。

**必须保留的边界：**首轮 Codex 用量超过单次预留，父控制器取得的是其自测摘要，没有取得首轮宿主 measurements；第二轮三项为宿主测量。因此严格的宿主反馈迭代门槛仍为 passed=false。事后重跑确认数字可复现，但不改变原始运行当时的事实。最终模型报告存在提升方向写反、模糊数值及混用耗时的问题；原文综述核验也超时。已修反馈来源标注和结果合同，尚无该补丁后的真实模型复验。

[最新复核报告](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r2-seeded/review/report.md) · [独立审计](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r2-seeded/review/audit.json) · [首轮原生复现](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r2-seeded/review/native-v1-reproduction.json) · [原始未通过指标](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r2-seeded/metrics.json)。原始条件、代码快照、来源、请求、执行收据和失败均在该目录，未覆盖。

该案例只证明发生了“反馈→方法修订→重跑”，不证明其全过程可靠、近期 Agent 论文已复现、新颖性成立或接近 SOTA。数据原有 2 条 train/dev 同文本；单 seed、开发集比较，评分模块保护还有可变依赖，具体边界见复核。较新 Agent 论文、宿主管理的实验 LLM 调用、V3 总档案整合、真实恢复和木屋界面交付仍待完成。

## 既有指标出处

| 简历内容 | 原始依据与适用范围 |
| --- | --- |
| Harness修复：5/16→12/16，协议失败9/16→0/16 | [固定SciFact修复报告](../../evals/reports/product-benchmark-repair-20260929/scifact-qa-r1/repair-summary.md)、[配对指标](../../evals/reports/product-benchmark-repair-20260929/scifact-qa-r1/paired-repair.json)：8个相同任务ID，两配置各8次，同DeepSeek和预算；baseline 2/8→6/8，candidate 3/8→6/8。修复后两臂通过相同，不证明重排策略额外收益；31.25%→75.00%是5/16→12/16的百分比表达，16次运行来自8道不同问题，不能说成16道独立题；不是全32题复测或官方榜单 |
| SciFact Recall@5 75%→87.5%、MRR@5 .604→.714 | [产品 benchmark](../../evals/reports/product-benchmark-20260929/report.md)：5,183 篇摘要，20 道有标准证据的保留题；生产 FTS5 对比 FTS5+BGE-M3+RRF，含各自分段，并非官方全量榜单 |
| QASPER Recall@5 44.46%→52.32% | 同一报告，28 道完整证据映射保留题；没有把无法映射证据的题算成通过 |
| 历史会话 Recall@5 8.33%→37.47% | 同一报告的 LongMemEval 25 道保留题及 [同 ID 历史条件](../../evals/reports/product-benchmark-20260929/paired-history/paired-condition.json)；度量召回标准历史会话，不是完整记忆问答准确率 |
| 记忆 20/20、压缩 22/24 | [历史真实能力专测](../../evals/reports/context-memory-optimization-20260918/report.md)、[原始指标](../../evals/reports/v3-context-memory-final/metrics.json)：Luna、自编场景；每个压缩案例执行两次，不是 Memory 开关消融收益 |
| 原文片段覆盖 91.67%→100%、引用 19/22→20/22 | [历史 Luna 配对](../../evals/reports/evidence-qa-20260927/report.md)：首组 16 题中的 12 道可回答题；两臂整体均 13/16，独立组均 14/16，不声称整体问答通过率提高 |
| 三条失败草稿仅续跑核验恢复 | [前两条](../../evals/reports/evidence-qa-20260927/verification-repairs/recovery-results.json)、[第三条](../../evals/reports/evidence-qa-20260927/code-fence-repair/recovery-results.json)：每条一次真实 Luna 核验、零新增工具/整答生成；不代表完整 Workbench 后处理重跑或总体恢复率 |
| 查询规划 Recall@5 +7.22 个百分点 | [策略报告](../../evals/reports/harness-strategies-20260921/report.md)：6 份文档，20 开发题、40 历史控制题（含 30 道可回答题），候选计量不全而未自动启用 |
| 320 条、592 检索、64 问答、24 工作流 | [产品 benchmark](../../evals/reports/product-benchmark-20260929/report.md)：296 检索任务×2配置=592；32题×2配置=64真实问答；24工程场景。运行数量不是通过数量 |

## 回归与版本

本轮最后完整后端 **433/433**（101.531 秒），[日志](../../evals/reports/auto-research-foundation-20260929/iteration-r2-review-regression/backend.log)与[运行记录](../../evals/reports/auto-research-foundation-20260929/iteration-r2-review-regression/backend.json)；前端 **33/33**，零跳过；随后区分纯执行摘要来源，控制器22项回归通过。这些是工程测试数，不是研究任务成功率。真实运行期产品源码与 condition.json 冻结哈希一致；summary 来源标注与 JSON 示例修复在运行结束后完成，不能归为本轮实验已验证效果。

历史 SciFact r4 已通过基础流程；reading-r3 原文简报局部修订后 11/11 段落通过模型核验，非 11 道独立任务或通读两篇论文；iteration-r1 的全部失败继续保留。旧完整问答 32 题两配置 9/32 与 7/32 不被固定八题修复成绩覆盖。主服务尚未加载本轮代码，.env 与用户主库未改。
