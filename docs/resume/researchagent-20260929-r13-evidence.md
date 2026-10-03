# r13 简历证据与面试说明

2026-09-29。对应[r13简历](researchagent-20260929-r13.md)；[r12](researchagent-20260929-r12.md)及此前版本保留。本版突出研究决策与 Agent-as-Tool，保留既有有效成绩，并同步原文阅读成功复验和方法迭代失败。基础流程对应提交1656832附近的冻结版本；最新阅读及迭代任务对应0a0a66d之后的工作区增量，各目录保留冻结源码指纹。各项评测不合并成新成功率。

## 面试时解释项目贡献

- **研究 Agent：**维护目标、原文证据、方案和状态，决定何时调研、委派实现、检查实测结果、继续研究或结束。
- **Codex CLI：**作为被调用的编码 Agent，负责交接范围内的实现与调试；接入、预算、持久任务、收据与反馈编排属于本项目工作，编码模型本身来自外部工具。
- **宿主执行：**运行真实实验、记录源码/数据/配置及指标来源；已有代码可单独测量，不必再次调用编码模型。
- **当前已证明：**一个真实课题贯通调研、委派、实验及报告，实际处理了编码失败后的产物复用；另一次真实原文简报在局部修订后通过逐段核验。已有检索、记忆和Harness指标分别证明对应机制在记录范围内的效果。
- **仍在完成：**依据科学结果修订方法、定向补调研和再次实验的真实闭环，以及统一成果档案和较新Agent研究课题适配。不能把编码调试或超限后换工具说成科学方法已迭代。

简历正文突出已实现功能与真实量化成果；本页保留样本规模、比较条件和失败。Auto Research 三组实验及300条claim表示实际验证范围，不是研究目标达成率或创新质量指标。

## 本次可以写进简历的成果

研究LLM实际调研并读取官方SciFact PDF，保存方法方案，调用Codex；两次编码分别超时/超过单次token预留。随后LLM自主使用run_experiments，宿主独立执行baseline、candidate、ablation，真实指标回到同一研究任务，LLM据此交付报告。完整基础流程验收通过，1185.5秒，2次CLI与1次纯测量任务均有收据。

[可读验收与复核报告](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r4/review/report.md) · [原始指标](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r4/metrics.json) · [原始报告](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r4/report.md) · [独立评分复核](../../evals/reports/auto-research-foundation-20260929/scifact-luna-r4/review/audit.json)。模型收到宿主结果后再结束的输入证据为该目录models/018/requests/001.json。

实验是300个官方dev claim的给定候选文档分类子任务；accuracy .45→.5233，macro-F1 .3403→.4288，消融等于基线。独立重跑与重算一致。正文只描述真实流程，未用这些分数证明研究Agent或Codex单独贡献，也未声称官方榜单成绩、SOTA或新颖性成立。

仍需说明：只有1版研究方案，没有据科学结果修改方法再实验的真实成功案例；调研子任务整篇回答核验失败，打开原文不等于所有结论核验通过；显式原论文阅读窗口18,000/61,158字符，不是通读。官方train/dev有2条相同文本；单seed、固定阈值、开发集比较；CONTRADICT类F1仍约.0612。原计时不含特征提取，不声称候选速度提升。V3总档案、真实中断恢复、较新Agent论文/多数据集案例尚待完善，主服务未加载本轮代码。

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
| 修复阶段完整后端432/432；前端33/33 | [后端日志](../../evals/reports/auto-research-foundation-20260929/iteration-repair-regression/backend.log)：99.083秒；分任务验收修复后另有42项相关回归，独立评分另有1项单测。前端新增实验计数、剩余预算与错误信息展示，33项通过。完整432项早于后续验收及独立评分增量，工程通过不代表方法迭代成功 |


## 当前工程验证与版本归属

修复阶段完整后端 **432/432**（99.083秒），日志见[iteration-repair-regression/backend.log](../../evals/reports/auto-research-foundation-20260929/iteration-repair-regression/backend.log)；后续分任务验收42项相关回归通过。新增独立评分单测另有记录。界面已区分编码/纯实验计数，展示剩余额度、结果校验错误与stderr，前端 **33/33**通过。本轮真实复验仍在运行，工程数量不代表研究成功率；主服务未重启。

**原文阅读：有单次成功，稳定性仍待提升。** [Luna reading-r3指标](../../evals/reports/auto-research-foundation-20260929/scifact-reading-luna-r3/metrics.json)显示226.234秒完成，13次工具调用，其中4次12000字符显式原文读取。首次模型核验10/11段落得到支持；局部修订把CPU线性分类器、词汇重叠rationale明确标为建议改造，不再写成文献已验证方法，第二次为11/11。见[交付简报](../../evals/reports/auto-research-foundation-20260929/scifact-reading-luna-r3/report.md)和[两次核验记录](../../evals/reports/auto-research-foundation-20260929/scifact-reading-luna-r3/verification.json)。这是一次真实功能成功，不是11道独立问答或人工金标准正确率，也不是整项Auto Research验收通过。

[阅读窗口审计](../../evals/reports/auto-research-foundation-20260929/scifact-reading-luna-r3/review/reading-window-audit.json)确认研究模型实际看到SciFact原文36000/61158字符、RerrFact原文12000/26673字符，0处无法解释的不一致；不是通读两篇全文。18次模型请求合计336175 token，输入/输出计量覆盖100%，缓存字段覆盖仅2/3，不声称节省成本。此前Luna reading-r2及DeepSeek恢复失败均保留；核验内部单次网络60秒、共享90秒的限制未因此改变。

**方法迭代：本次失败，不能升级简历结论。** [iteration-r1指标](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r1/metrics.json)记录1064.671秒、1版方案、3次CLI及2次纯实验执行，0项有效测量，outcome=blocked、passed=false。它要求根据第一轮真实指标修改方法并重跑，但未进入该阶段。原始编码/实验收据记录了超时、单次token超限、错误的sklearn导入以及把嵌套混淆矩阵放入只接受有限数值的metrics对象；后者即使进程退出码为0也被拒绝。见[原始任务与收据](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r1/coding-tasks.json)及[迭代判定](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r1/method-iteration.json)。其调研子任务仍核验失败，说明独立reading-r3成功尚不足以证明完整研究流程稳定。

本轮已补有界stderr回传与剩余额度，并修复验收漏识别排队save_plan、分任务对比/消融的误判。只读预算审计确认iteration-r1结束时仍剩1次编码、572秒、808333 token额度和1795秒实验时间；旧提示把单次超額混同累计耗尽，现已纠正。数值metrics与diagnostics明确分开，无效原始JSON也保留。最新独立评分脚本用官方gold复核了失败基线的300条预测，数字一致，但不改变该任务格式失败、未收到有效反馈的结论。新隔离复验scifact-iteration-luna-r2-seeded正在运行，仅复用上一轮两个生成脚本，重新测量全部实验；没有新方法迭代成功结论。见[失败复核与修复](../../evals/reports/auto-research-foundation-20260929/scifact-iteration-luna-r1/review/report.md)。

较新Agent论文案例已做[A-MEM源码适配准备](../auto-research-agent-case-selection-20260929.md)，尚未复现。当前实验沙箱不具备供实验算法调用模型的受控接口；上游Agent记忆构建和问答依赖真实LLM，需补齐该能力及相应预算/收据后再验收。

原生文件交接故障有对照：沙箱账户创建的文件无法由宿主直接设置deny-write ACL；相同内容的宿主副本可以。执行器用字节不变的原子交接继续施加只读保护。三组原生复现和保护写入拒绝见[native-measurement-r2](../../evals/reports/auto-research-foundation-20260929/native-measurement-r2/report.md)。没有扩大权限、删除失败或修改用户主库/.env。

历史r1/r2/r3受阻结果原样保留；旧完整DeepSeek32题的9/32与7/32也保留，固定八题修复的5/16→12/16不替换它。简历可以使用这些真实范围内的成果，后续以更完整的研究迭代案例更新。
