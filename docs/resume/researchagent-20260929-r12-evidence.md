# r12 简历证据与面试说明

2026-09-29。对应[r12简历](researchagent-20260929-r12.md)；[r11及既有指标边界](researchagent-20260929-r11-evidence.md)保留。当前版本新增真实Auto Research基础流程成果，不把生成候选方法的分数当作自研Harness独立收益。

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
| 416/416 后端、32/32 前端 | [第二次修复回归](../../evals/reports/auto-research-foundation-20260929/repair2-regression.json)、[后端日志](../../evals/reports/auto-research-foundation-20260929/repair2-backend.log)、[前端日志](../../evals/reports/auto-research-foundation-20260929/repair2-frontend.log)。后端108.927秒，两者均实际重跑；工程通过不代表研究目标成功 |


## 当前工程验证与版本归属

最新完整回归420/420后端、32/32前端，operation元数据补测22项通过：[修复记录](../../evals/reports/auto-research-foundation-20260929/repair3-regression.json)。r4按此冻结源码运行并保留ZIP。r4结束后发现“超时反馈没有列出已生成文件”会让研究LLM误以为没有产物，已补保存部分代码/进度；相应CodingTool/控制器/验收38项通过，见[增量回归](../../evals/reports/auto-research-foundation-20260929/repair3-timeout-artifacts.log)。此补丁未冒充已经包含在r4实测中。

原生文件交接故障有对照：沙箱账户创建的文件无法由宿主直接设置deny-write ACL；相同内容的宿主副本可以。执行器用字节不变的原子交接继续施加只读保护。三组原生复现和保护写入拒绝见[native-measurement-r2](../../evals/reports/auto-research-foundation-20260929/native-measurement-r2/report.md)。没有扩大权限、删除失败或修改用户主库/.env。

历史r1/r2/r3受阻结果原样保留；旧完整DeepSeek32题的9/32与7/32也保留，固定八题修复的5/16→12/16不替换它。简历可以使用这些真实范围内的成果，后续以更完整的研究迭代案例更新。
