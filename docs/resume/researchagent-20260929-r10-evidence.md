# r10 简历证据与实现边界

日期：2026-09-29。对应 [r10 简历](researchagent-20260929-r10.md)；[r9](researchagent-20260929-r9.md)、r8和更早版本保留。本次修改措辞与实现状态，不重算或覆盖历史成绩。

## 本版变化

1. 按“研究工作台＋Agent Harness＋可选 Auto Research”组织项目定位。Codex 是研究 LLM 可调用的实现工具，编码模型本身的能力不作为自研贡献。
2. 保留已有真实量化结果；恢复指标解释为复用检索与草稿，避免重跑；反馈指标注明30道可回答题，记忆召回不写成回答准确率。
3. 工程回归数量移到本证据说明，简历主体突出能力和效果。409/409后端与32/32前端表示测试结果，不表示科研成功率。
4. 第二次完整真实课题已结束，仍未通过。简历只写工具编排已实现、完整课题联调中；没有新增问答提升或自主科研成功指标。

## 指标出处

| 简历内容 | 原始依据与适用范围 |
| --- | --- |
| SciFact Recall@5 75%→87.5%、MRR@5 .604→.714 | [产品 benchmark](../../evals/reports/product-benchmark-20260929/report.md)：5,183 篇摘要，20 道有标准证据的保留题；生产 FTS5 对比 FTS5+BGE-M3+RRF，含各自分段，并非官方全量榜单 |
| QASPER Recall@5 44.46%→52.32% | 同一报告，28 道完整证据映射保留题；没有把无法映射证据的题算成通过 |
| 历史会话 Recall@5 8.33%→37.47% | 同一报告的 LongMemEval 25 道保留题及 [同 ID 历史条件](../../evals/reports/product-benchmark-20260929/paired-history/paired-condition.json)；度量召回标准历史会话，不是完整记忆问答准确率 |
| 记忆 20/20、压缩 22/24 | [历史真实能力专测](../../evals/reports/context-memory-optimization-20260918/report.md)、[原始指标](../../evals/reports/v3-context-memory-final/metrics.json)：Luna、自编场景；每个压缩案例执行两次，不是 Memory 开关消融收益 |
| 原文片段覆盖 91.67%→100%、引用 19/22→20/22 | [历史 Luna 配对](../../evals/reports/evidence-qa-20260927/report.md)：首组 16 题中的 12 道可回答题；两臂整体均 13/16，独立组均 14/16，不声称整体问答通过率提高 |
| 三条失败草稿仅续跑核验恢复 | [前两条](../../evals/reports/evidence-qa-20260927/verification-repairs/recovery-results.json)、[第三条](../../evals/reports/evidence-qa-20260927/code-fence-repair/recovery-results.json)：每条一次真实 Luna 核验、零新增工具/整答生成；不代表完整 Workbench 后处理重跑或总体恢复率 |
| 查询规划 Recall@5 +7.22 个百分点 | [策略报告](../../evals/reports/harness-strategies-20260921/report.md)：6 份文档，20 开发题、40 历史控制题（含 30 道可回答题），候选计量不全而未自动启用 |
| 320 条、592 检索、64 问答、24 工作流 | [产品 benchmark](../../evals/reports/product-benchmark-20260929/report.md)：296 检索任务×2配置=592；32题×2配置=64真实问答；24工程场景。运行数量不是通过数量 |
| 409/409 后端、32/32 前端 | [修复回归](../../evals/reports/auto-research-foundation-20260929/repair-regression.json)、[后端日志](../../evals/reports/auto-research-foundation-20260929/repair-backend.log)、[前端日志](../../evals/reports/auto-research-foundation-20260929/project-frontend.log)。后端104.476秒；前端为本次修复前同代码的结果。工程通过不代表研究目标成功 |

## 新工具链的证据强度

当前 [AutoResearch](../../research_agent/auto_research.py)、[CodingTool](../../research_agent/coding_tool.py)、[项目准备](../../research_agent/research_project.py) 已在 Workbench、HTTP 和现有任务界面接通。控制器使用 SQLite 检查点保留方案、工具结果、研究/编码子任务与预算；研究 LLM 可选择下一步。现有受控检索实验的真实 Codex 执行证据见 [9月27日报告](../../evals/reports/evidence-qa-20260927/report.md)。

新链路已完成实际原生依赖安装与执行：[native-r1](../../evals/reports/auto-research-foundation-20260929/native-r1/result.json)，安装 packaging==25.0 后在原生沙箱运行，0模型调用。真实 Luna 意图测试 [routing-luna-r1](../../evals/reports/auto-research-foundation-20260929/routing-luna-r1/result.json) 为 7/7；只证明这7例路由结果符合预期，不是大样本零误启动率或全链路验收。

控制器测试覆盖“实测退化→补调研→改方案→再次委派”，但其编码结果为测试夹具。SciFact真实课题现已结束：[metrics.json](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r1/metrics.json)、[原报告](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r1/report.md)。耗时736.235秒、3次研究子任务、1个方案版本、1次编码工具请求，最终outcome=blocked，完整功能验收未通过，没有有效baseline/candidate/ablation测量。

三个研究子任务分别因policy_denied、answer_verification_failed、invalid_answer_patch失败。模型准备了官方仓库固定提交及numpy/scikit-learn依赖，但把目录references/scifact-official传入protected_files；工具按普通文件读取而在CLI启动前失败。失败state只有error，没有coding_started或Codex收据；其600秒配额已被计入累计预算，修正委派因剩余额度不足被拒绝。因此“编码工具调用1次”不等于“Codex实际编码1次”；metrics中的model_received_experiment_results=true实际表示模型收到编码任务终态失败信息，不是拿到了真实实验指标。保留首次失败，不修饰成研究成功。

第二次真实课题 [r2指标](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r2/metrics.json) 与 [运行复核](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r2/review-notes.md) 已归档：1311.078秒、2个研究子任务、1版方案、2次编码委派，最终blocked，无有效baseline/candidate/ablation实测，完整功能验收未通过。原始Codex收据显示两次进程确已启动，分别540.063秒、60.047秒超时；第一轮多次文件写入失败，超时后又暴露输出解析错误。原metrics的cli_executions=0漏计了在汇总前解析失败的执行，不能据此说CLI未启动；错误原记录保留并附复核，不修改为成功。

主服务尚未重启，因此“当前代码已接通”不等于用户现有浏览器服务已经部署该版本。通用项目的中断恢复、V3档案整合、真实消融与结果驱动修订仍需完整验收；当前宿主检查数值、配置和入口哈希不能代替独立复核实验评分逻辑。

## 保留的负结果

最新完成的 DeepSeek 完整问答仍为基线 **9/32**、候选 **7/32**，两臂各21次多工具协议失败；另有原文已读但可见引用不足。修复代码已加入，固定SciFact八题两配置复测正在进行，尚无完整复核结论，不声称整体回答质量已改善，也不把成功重跑覆盖旧结果。[失败分析](../../evals/reports/product-benchmark-20260929/findings.md)继续保留。

旧手写文本规则得到的混合 benchmark 指标已经撤销产品解释，未采用；原排序候选 Recall@5 不归因自研研究 Agent 或 Codex 单独贡献。目标与下一步见 [目标差距](../auto-research-goal-gap-20260929.md)。
