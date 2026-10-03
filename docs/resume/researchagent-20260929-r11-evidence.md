# r11 简历证据与实现边界

日期：2026-09-29。对应 [r11 简历](researchagent-20260929-r11.md)；[r10](researchagent-20260929-r10.md)保留；[r9](researchagent-20260929-r9.md)、r8和更早版本保留。本次修改措辞与实现状态，不重算或覆盖历史成绩。

## 本版变化

1. 新增与自研Harness直接相关的真实修复结果：固定SciFact 8题×2配置，修复前后通过5/16→12/16，多工具协议失败9/16→0/16。比较的是产品修复版本，未把Codex编码能力归为本项目收益。
2. 此16次问答属于冻结修复版，源文件及模型响应完整归档；后续编码收据、恢复和界面修复另列，未冒充同批问答评测代码。
3. 最新工程回归416/416后端、32/32前端。原生写入诊断定位了补丁工具与shell路径的差异，验证同权限shell写入成功且保护文件写入仍被拒绝。
4. SciFact Auto Research r3已结束：727.812秒，outcome=blocked，2次真实CLI调用，没有有效实验测量，完整功能验收未通过。首个研究子任务因model_error未完成核验；两条编码任务均记录token预算超限后未启动实验，原始失败保留。完整自主研究仍写为联调验收中，不提前宣称研究成功或SOTA。

## 指标出处

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
| 416/416 后端、32/32 前端 | [第二次修复回归](../../evals/reports/auto-research-foundation-20260929/repair2-regression.json)、[后端日志](../../evals/reports/auto-research-foundation-20260929/repair2-backend.log)、[前端日志](../../evals/reports/auto-research-foundation-20260929/repair2-frontend.log)。后端108.927秒，两者均实际重跑；工程通过不代表研究目标成功 |

## 新工具链的证据强度

当前 [AutoResearch](../../research_agent/auto_research.py)、[CodingTool](../../research_agent/coding_tool.py)、[项目准备](../../research_agent/research_project.py) 已在 Workbench、HTTP 和现有任务界面接通。控制器使用 SQLite 检查点保留方案、工具结果、研究/编码子任务与预算；研究 LLM 可选择下一步。现有受控检索实验的真实 Codex 执行证据见 [9月27日报告](../../evals/reports/evidence-qa-20260927/report.md)。

新链路已完成实际原生依赖安装与执行：[native-r1](../../evals/reports/auto-research-foundation-20260929/native-r1/result.json)，安装 packaging==25.0 后在原生沙箱运行，0模型调用。真实 Luna 意图测试 [routing-luna-r1](../../evals/reports/auto-research-foundation-20260929/routing-luna-r1/result.json) 为 7/7；只证明这7例路由结果符合预期，不是大样本零误启动率或全链路验收。

控制器测试覆盖“实测退化→补调研→改方案→再次委派”，但其编码结果为测试夹具。SciFact真实课题现已结束：[metrics.json](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r1/metrics.json)、[原报告](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r1/report.md)。耗时736.235秒、3次研究子任务、1个方案版本、1次编码工具请求，最终outcome=blocked，完整功能验收未通过，没有有效baseline/candidate/ablation测量。

三个研究子任务分别因policy_denied、answer_verification_failed、invalid_answer_patch失败。模型准备了官方仓库固定提交及numpy/scikit-learn依赖，但把目录references/scifact-official传入protected_files；工具按普通文件读取而在CLI启动前失败。失败state只有error，没有coding_started或Codex收据；其600秒配额已被计入累计预算，修正委派因剩余额度不足被拒绝。因此“编码工具调用1次”不等于“Codex实际编码1次”；metrics中的model_received_experiment_results=true实际表示模型收到编码任务终态失败信息，不是拿到了真实实验指标。保留首次失败，不修饰成研究成功。

第二次真实课题 [r2指标](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r2/metrics.json) 与 [运行复核](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r2/review-notes.md) 已归档：1311.078秒、2个研究子任务、1版方案、2次编码委派，最终blocked，无有效baseline/candidate/ablation实测，完整功能验收未通过。原始Codex收据显示两次进程确已启动，分别540.063秒、60.047秒超时；第一轮多次文件写入失败，超时后又暴露输出解析错误。原metrics的cli_executions=0漏计了在汇总前解析失败的执行，不能据此说CLI未启动；错误原记录保留并附复核，不修改为成功。

主服务尚未重启，因此“当前代码已接通”不等于用户现有浏览器服务已经部署该版本。通用项目已接上显式中断恢复，完整收据复用与未知执行拒绝重放有新增回归；V3档案整合、真实消融与结果驱动修订仍需完整验收；当前宿主检查数值、配置和入口哈希不能代替独立复核实验评分逻辑。

## 保留的负结果

最新完成的 DeepSeek 完整问答仍为基线 **9/32**、候选 **7/32**，两臂各21次多工具协议失败；另有原文已读但可见引用不足。固定SciFact八题两配置复测已完成，按同一历史评分规范得到5/16→12/16；其余24题未重跑，不能据此改写原全32题成绩。5条评分响应的字段类型按已有规则规范化，原始模型响应及首次schema失败保留，没有新增评分调用。四条未通过仍归档。[失败分析](../../evals/reports/product-benchmark-20260929/findings.md)继续保留。

旧手写文本规则得到的混合 benchmark 指标已经撤销产品解释，未采用；原排序候选 Recall@5 不归因自研研究 Agent 或 Codex 单独贡献。目标与下一步见 [目标差距](../auto-research-goal-gap-20260929.md)。

## 本次简历与目标说明同步

当前正文使用通过率并保留分母，突出工具协议与核验修复对任务交付的作用；Codex作为现成编码工具集成，不用其生成排序器的成绩代表研究Agent贡献。普通工作台、可选Auto Research和未验收的完整科研闭环分开说明。更新过程只修改文档，没有改变冻结运行中的产品源码、主服务、用户数据库或.env。

第三次真实课题终态：[r3指标](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r3/metrics.json)、[r3报告](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r3/report.md)。job_status=completed表示研究任务已结束，outcome=blocked和functional_path_completed=false才说明目标未完成；model_received_experiment_results=false。研究模型报告中的实现/环境解释需以编码收据及代码复核为准，不用其文字推断已经完成实验。

## 后续执行修复（不新增科研收益指标）

新增已有代码直接测量工具run_experiments、完整收据预算结算和字节一致的宿主只读交接，复用原执行器。完整后端420/420、前端32/32；最终operation统计22项补测通过，见[修复回归](../../evals/reports/auto-research-foundation-20260929/repair3-regression.json)。同r3代码的三组宿主复现与真实写保护负例见[原生复核](../../evals/reports/auto-research-foundation-20260929/native-measurement-r2/report.md)；候选无提升，未作为研究Agent增益。r4完整课题运行中，仍无通过结论；主服务尚未加载本轮实现。
