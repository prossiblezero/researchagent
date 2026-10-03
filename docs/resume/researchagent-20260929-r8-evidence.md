# r8 简历证据与实现范围

日期：2026-09-29。对应 [r8 简历](researchagent-20260929-r8.md)。本版简历引用已有真实评测，不新增或重算模型成绩；同时核对当前未提交代码，区分工具基础、已接通功能与待实现目标。r7 和此前版本保留。

## 本版调整

- Coding Agent 条目改写为已经实现的执行适配、测量、预算、恢复等工程能力，不再用排序候选 Recall@5 88.89%→100% 证明研究 Agent 贡献；原结果及失败档案仍保留。
- 记忆和压缩补入真实历史专测；反馈策略补入具体查询规划候选的历史检索收益。它们不是本轮新成绩，也不是所有功能的独立因果证明。
- 恢复条目解释为只续跑核验并复用成果，不声称节省特定百分比的时间/费用。
- 区分已验收的 Codex 执行子系统与正在真实课题验收的自主研究闭环。持久 Coding Agent 工具、研究控制器、工作台入口与研究模型工具集已经初步接通；无需为工具接入单独追求量化提升。下方记录最新验证范围，不将正在运行的完整课题写成已通过。
- 记忆条目补充实际生产检索在 LongMemEval 历史会话上的对照收益；明确为会话 Recall@5，不写成记忆问答正确率。注册实验最多3轮是功能上限，不当作效果指标。

## 数据来源与口径

| 条目 | 原始依据 | 范围 |
| --- | --- | --- |
| 三条真实核验恢复 | [前两条](../../evals/reports/evidence-qa-20260927/verification-repairs/recovery-results.json)、[第三条](../../evals/reports/evidence-qa-20260927/code-fence-repair/recovery-results.json) | 每条1次真实Luna核验，0新工具/整答生成；只续跑 Agent Loop 核验，没有完整重跑 Workbench 后处理；不能推为总体恢复率100% |
| SciFact/QASPER检索 | [真实产品报告](../../evals/reports/product-benchmark-20260929/report.md) | 同资料池/top5/预览预算，生产FTS5对比生产FTS5+BGE-M3+RRF，包含各自分段；SciFact保留30题中20题有gold，QASPER保留40题中28题具备完整映射；MRR为MRR@5，不是官方全榜 |
| 原文与引用 | [2026-09-27报告](../../evals/reports/evidence-qa-20260927/report.md) | 两组32题共64次历史Luna配对；简历分项取首组16题中的12道可回答题。首组两臂整体均13/16，独立组均14/16；不能把分项改善写成整体通过率提高 |
| 记忆20/20、压缩22/24、协议24/24 | [上下文记忆报告](../../evals/reports/context-memory-optimization-20260918/report.md)、[最终专测指标](../../evals/reports/v3-context-memory-final/metrics.json) | 2026-09-18真实Luna自编能力测试；含召回、更新、时效、遗忘、多事实，每个压缩案例执行两次。不是官方LongMemEval得分，不是开关Memory/压缩后的任务收益对照 |
| 历史会话Recall@5 8.33%→37.47% | [真实产品报告](../../evals/reports/product-benchmark-20260929/report.md)、[同ID历史协议](../../evals/reports/product-benchmark-20260929/paired-history/paired-condition.json) | LongMemEval 25道保留题；从同一SQLite历史快照复制两臂，比较FTS5与FTS5+BGE-M3+RRF。度量找到标准历史会话的召回，不是完整LongMemEval问答准确率，也不证明三级记忆/压缩各自的因果收益 |
| 查询规划+7.22个百分点 | [策略评测报告](../../evals/reports/harness-strategies-20260921/report.md) | 6份文档、60题×4组；20开发题、40历史控制题含30可回答题。收益属于查询规划候选；完整问答5/8→6/8含核验超时影响，不作纯检索因果收益。计量不全未通过启用门槛，默认保留 |
| CLI执行、有限迭代与恢复 | [当前执行合同](../v4-controlled-experiments.md)、[真实验收](../../evals/reports/evidence-qa-20260927/report.md) | 当前只执行注册检索实验；真实案例验证无改善停止及开发达标停止，保存负结果。不代表任意研究项目自动复现、训练或达到SOTA |
| 320条、592检索、64问答、24工作流、370/31回归 | [产品报告](../../evals/reports/product-benchmark-20260929/report.md)、[回归](../../evals/reports/product-benchmark-20260929/regression.json) | 320=296检索任务+24工程场景；296×2=592检索，另取32题×2=64问答。运行数量不是通过数量，测试通过不等于模型回答正确 |

缓存历史专测也可面试展开：[上下文报告](../../evals/reports/context-memory-optimization-20260918/report.md)的DeepSeek固定前缀缓存输入占比为62.5%，变化前缀为0%，相关字段覆盖100%；不能直接换算成费用节省62.5%。正文为控制长度未再增加该项。

## 必须能解释的当前问题

2026-09-29完整DeepSeek问答，基线通过9/32、原文重排候选7/32；两臂各21次因一次返回多个工具调用而被适配层拒绝。另有已经读到正确原文但最终可见引用未同步的问题。当前代码已增加多工具串行消费和显式预览引用检查，但修复后的真实模型复测尚未完成；不能因此重算旧成绩或声称整体Agent通过率提升，详见[失败记录](../../evals/reports/product-benchmark-20260929/findings.md)。Luna新试跑因服务过载单列，不混入上述成绩。

9月28日手写规则得到的61.55%→62.83%和MRR .560→.611已撤销产品解释，不用于任何r8条目。源码与原始记录保留。

产品目标是日常聊天、按需调研与可选Auto Research共存。已新增[研究控制器](../../research_agent/auto_research.py)、[项目准备](../../research_agent/research_project.py)、[CodingTool](../../research_agent/coding_tool.py)和相应tests。Workbench与HTTP入口已接通AUTO_RESEARCH，模型可选择调研、保存方案、准备公开源码/数据/固定wheel依赖、委派编码、检查结果与结束。控制器测试覆盖实验结果导致补调研、修订方案和再次委派；这些测试中的编码结果是夹具，不能冒充真实研究效果。

新增真实验证：[原生依赖环境](../../evals/reports/auto-research-foundation-20260929/native-r1/result.json)实际安装packaging==25.0并在原生沙箱中完成运行；[Luna路由](../../evals/reports/auto-research-foundation-20260929/routing-luna-r1/result.json)7/7场景符合预期，包含普通聊天、只调研、仅idea、CVPR方向探索、明确自动研究和状态询问。它们只验证工程通路与这7例意图边界，不证明科研质量。各次源码与任务已经按运行指纹归档。真实SciFact课题运行保存在`evals/reports/auto-research-foundation-20260929/scifact-luna-r1`，结论须以最终metrics和人工代码/原文复核为准。

当前工作区补跑[后端完整回归](../../evals/reports/auto-research-foundation-20260929/report.md)：381/381通过，96.097秒。简历正文370/31仍明确对应真实产品benchmark冻结版本；新工程回归不修改该版本成绩，也不替代真实模型验收。

目标、当前能力和验收里程碑见 [差距说明](../auto-research-goal-gap-20260929.md)；实施方向见 [V4修订](../auto-research-v4-redesign-20260929.md)。不因简历需要额外强做54次归因对照。
