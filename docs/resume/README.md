# 当前投递稿

[ResearchAgent 简历正文（2026-10-20，三项目简历精简版）](researchagent-20261020.md) · [指标证据与面试说明](researchagent-20261009-evidence.md)

本版按一份简历容纳三个项目的篇幅重写：项目描述以“论文调研与实验验证需求→资料分散、上下文受限与人工迭代问题→Agent 工作台设计”组织；主要工作由五条缩为意图路由、Agentic RAG 与评测、记忆与经验复用、Multi-Agent 自动研究四条。保留关键机制与对应结果，移除代码行数、测试数量、重复背景及次要指标。记忆单独成条；第2条明确保留 LLM-as-a-Judge 四维离线评测与裁判校准，区别于运行时的引用核验。第4条明确主控—Worker 分层编排、并行调研与编码委派，协作范式及角色分工见证据附件。80 条受控样例的裁判准确率 92.5% 留在证据附件用于追问；详细实现、执行框架及失败分析继续在展开稿中准备。本次只调整表达与篇幅，未新增实验或替换指标。

## 2026-10-18 历史展开版本

[ResearchAgent 简历正文（2026-10-18，加入决策模型式意图路由）](researchagent-20261018.md) · [指标证据与面试说明](researchagent-20261009-evidence.md)

本版沿用“技术栈＋项目描述＋五条主要工作”，每条按“问题→做法→结果”组织。在 2026-10-16 的 STAR 改写稿（[researchagent-20261016.md](researchagent-20261016.md)）基础上，第1条加入意图路由升级：本地 Qwen3-0.6B 选项打分＋LoRA，低置信与自动研究交 LLM 确认。指标取 160 题新保留集 v3 的首次运行：准确率 98.75%（原 LLM 路由 97.50%，McNemar p=0.6875，只写持平不写提升），自动研究误触发 0/134，完整路由 p50 6.39s→3.99s，远程请求 168→98。原 210 题的 93.81% 由新保留集成绩取代，归档不变。技术栈补 PyTorch、LoRA，测试数更新为导出副本的 750 项。依据见[证据附件](researchagent-20261009-evidence.md)“决策模型式意图路由”一节及[优化历程第 13 阶段](../optimization-history/13-decision-router.md)。

2026-10-16 STAR 改写稿相对 10-09 稿的变化：删去 SciFact 6 题 100% 与团队论文 10 题 80% 两个小样本数字；LongMemEval Recall@5 因拒答题计分问题（见[项目不足总结](../project-shortcomings-20261013.md)第 16 项）暂不引用；第5条补充裁判校准与 24 题阶段对照两个结果。

## 2026-10-09 历史投递版本

[ResearchAgent 简历正文（2026-10-09，最终实现与项目成果）](researchagent-20261009.md) · [指标证据与面试说明](researchagent-20261009-evidence.md)

本版采用“技术栈＋项目描述＋五条主要工作”。第2条为“Agentic RAG 与跨文档问答”，说明按证据调整查询、交替检索与原文阅读、核验跨文档结论；第3条为“记忆与经验学习”，将历史检索、事实/偏好管理、上下文压缩和受JitRL启发的过程经验复用作为完整设计；第4条为“Auto Research 与多 Agent 协作”，展示自动生成候选方案的任务效果：SciFact开发集证据分类macro-F1为45.04%→49.00%（+3.96个百分点），HotpotQA 64题证据检索Recall@2为59.38%→65.63%（+6.25个百分点），多Agent专项核验与实验记录保留在证据附件。正文第1条仍采用14题超长资料问答子集：同模型与单次16K上下文预算下，Agent完成率85.7%（12/14），单次API＋Prompt为35.7%（5/14）；其中SciFact的6题以5,183篇摘要为语料，完成率100%，同条件API＋Prompt为33.3%。分组规则、评分依据和历史结果保留在证据附件。

2026-10-12 接入参数冻结下的经验提炼与后续检索复用。记忆条目以原始关键词记忆为比较起点，本次LongMemEval 25题复测Recall@5为8.3%→33.5%，旧37.5%保留为历史记录；20道同源后续问题、40次首次运行的严格完成率为65%→75%（3题改善、1题退化），平均作答token增加2.0%，平均耗时减少7.5%。两项指标分别计分，整体收益不单独归因于过程经验。设计、成本与逐题结果见[记忆与经验学习评测](../../evals/reports/memory-learning-20261012/report.md)。

2026-10-10 补测原始资料＋单次API＋Prompt基线（本地档案：`research-notes/direct-api-24-20261010/report.md`）：同24题流式实测12/24（50.0%），优化版Agent历史结果18/24（75.0%）。直接API输入采用单次16K预算下的固定语料前缀，9道可答题缺关键证据；完整资料子集直接API为7/10、Agent为6/10，不能据总分声称纯推理能力提升。原简历25%仍指旧版Agent；非流式传输失败轮单独保留。详见证据附件。

2026-10-11 完成两条核验故障的专项恢复，累计完成记录18＋2＝20/24。首次运行与累计记录分别保留，正文不新增基线比较；详见[结果口径报告](../research/rag-comparison-20261011.md)和[优化历程](../optimization-history/12-agentic-rag-and-verification-recovery.md)。

正文第1条的结果可用[项目价值与具体案例](researchagent-value-20261010.md)在面试中展开：在预先定义的14题超长资料子集中，单次原文前缀API基线35.7%，项目85.7%，差50个百分点；强调从大资料库定位和组织证据，并展示实际检索、原文阅读及修订链路。

第2条先写 Agentic RAG 的多轮检索、阅读和核验，再写自建团队论文多跳检索与关系问答任务集的任务完成率80%，随后介绍混合检索实现及 SciFact 指标。10题分母、6/10→8/10严格完成和8/10→10/10成功交付的前后对照保留在[证据附件](researchagent-20261009-evidence.md)及优化后报告（本地档案：`research-notes/author-relations-20261010/report.md`）。语料为五篇团队论文与一篇干扰论文；q04、q10虽成功交付，仍有内容或引用缺项。该成绩与 SciFact 检索指标、24题整体任务完成率分别记录。

历史31.25%→75.0%与近期70.8%→75.0%分别保留为阶段记录，见[优化进展](../optimization-history/10-original-reading-and-delivery.md)及评测报告（本地档案：`research-notes/subset-e2e-20261009/final-report.md`）。该09-29旧版源码已独立恢复（本地档案：`research-notes/baseline-recovery-20261009/report.md`），并完成同24题重新实测（本地档案：`research-notes/legacy-24-20261009/report.md`），旧版新成绩不预设为历史31.25%。上一版[2026-10-07 场景版](researchagent-20261007.md)、[2026-10-03 六条稿](researchagent-20261003-r2.md)、[四条稿](researchagent-20261003.md)及更早版本保留。

以上 10-09 段落及以下均为历史版本记录，当前投递仅使用最上方正文链接。

## 2026-09-30 历史投递版本：r30

文案：[r30：证据驱动研究 Agent / Auto Research](researchagent-20260930-r30.md)；[详细证据与面试说明](researchagent-20260930-r30-evidence.md)。r29 及此前版本保留。

r30 将投递正文收回到简历粒度：一句项目定位、四条能力与结果；r31 进一步精简措辞，仍为四条主线。

# 2026-09-30 历史投递版本：r29

文案：[r29：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r29.md)；证据说明沿用 [r29 evidence](researchagent-20260930-r29-evidence.md)。r29 将正文收缩为五条核心能力；r30 进一步压缩成四条，作为当前投递稿。

# 2026-09-30 历史投递版本：r28

当前文案：[r28：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r28.md)；[证据与面试说明](researchagent-20260930-r28-evidence.md)。r27 及此前版本保留。

r28 在 r27 的两个真实课题证据基础上，补入 `assess_results` 结果决策层：只使用宿主有效测量和同条件 baseline/candidate 配对，记录达标、改善、退化、无测量和无目标时的下一步建议。该功能已有 454/454 主回归通过；没有把代码测试写成真实模型跨任务收益。SciFact 的反馈迭代与 QASPER 的结构复核继续保留原始失败、指标审计和科学边界。

# 2026-09-30 历史投递版本：r27

当前文案：[r27：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r27.md)；[证据与面试说明](researchagent-20260930-r27-evidence.md)。保留 r26、r25、r24 及此前版本。

r27 根据 QASPER 第二独立课题的真实运行状态更新：补入固定 24 条任务、3 个 seed；独立审计纠正原报告把 Hit@5 记成 Recall@5，严格 Recall@5 为首轮 baseline/candidate **0.5073/0.4657**、第二轮均 **0.5073**；同时修复验收器对跨轮方法源码变化的误判，并用原始收据独立复核结构反馈迭代路径。SciFact r2 的真实源码变化、冻结评分器/数据、独立 v2 消融和单 seed 限制继续保留。r27 不把 QASPER 的未改善结果包装成科学收益。

## 2026-09-30 历史投递版本：r26

当前文案：[r26：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r26.md)；[证据与面试说明](researchagent-20260930-r26-evidence.md)。保留 r25、r24 及此前版本。

r26 根据 QASPER 第二独立课题的真实运行状态更新：补入固定 24 条任务、3 个 seed、功能路径完成但反馈迭代未通过的结果；独立审计纠正原报告把 Hit@5 记成 Recall@5 的口径，严格 Recall@5 为首轮 baseline/candidate **0.5073/0.4657**、第二轮均 **0.5073**。同时保留 SciFact r2 的真实源码变化、冻结评分器/数据、独立 v2 消融和单 seed 限制。r26 不把 QASPER 的未改善结果包装成科学收益。

## 2026-09-30 历史投递版本：r25

当前文案：[r25：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r25.md)；[证据与面试说明](researchagent-20260930-r25-evidence.md)。保留 r24 及此前版本。

r25 根据真实 Luna 反馈迭代 r2 更新：跨轮只修改算法源码，评分器与数据保持冻结，独立 v2 消融完成；仍明确单 seed、开发集和 `scientific_effect_verified=false` 的科学边界。

## 2026-09-30 历史投递版本：r24

当前文案：[r24：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r24.md)；[证据与面试说明](researchagent-20260930-r24-evidence.md)。保留 r23 及此前版本。

r24 根据最新真实验收重写 Auto Research 主条目：明确 R4 功能路径通过、一次反馈迭代路径通过，以及 `scientific_effect_verified=false`、严格消融不完整等边界。Codex 继续定位为 Agent 可调用的编码工具；普通聊天和单次调研仍保持轻量独立。

## 2026-09-30 历史投递版本：r23

当前文案：[r23：证据驱动研究 Agent 与按需 Auto Research](researchagent-20260930-r23.md)；[证据与面试说明](researchagent-20260930-r23-evidence.md)。保留此前版本。

r23 是 r22 的表达升级：明确普通聊天/调研与按需 Auto Research 的边界，明确 Codex 是研究 Agent 可调用的编码工具，明确项目收益来自证据约束、研究决策、宿主测量、反馈、恢复和可追溯性。保留 R2 的真实两版方案与指标方向不一致结果；R3 的上下文超限、核验不可用和编码修复失败继续作为可靠性边界，不包装成科学收益。没有新增指标或改变原始报告。

## 2026-09-30 历史投递版本：r22

当前文案：[r22：证据驱动技术研究 Agent 与按需 Auto Research](researchagent-20260930-r22.md)；[证据与面试说明](researchagent-20260930-r22-evidence.md)。保留此前版本。

r22 在 r21 基础上明确普通调研与按需 Auto Research 的边界，并补入最新 R3 上下文回归：R2 的 SciFact 固定 cited-doc 子任务完成两版方案、8 组有效宿主 measurement，以及一次真实结果回传→方法修订→重跑；accuracy 有提升但 macro-F1/CONTRADICT F1 下降，预算耗尽且 `passed=false`。R3 记录上下文超限、核验不可用和编码修复失败，不能包装成科学收益；文案只把它作为闭环可靠性边界。

# 2026-09-30 历史投递版本：r19

当前文案：[r19：证据驱动研究 Agent、Agent-as-Tool 与宿主模型实验](researchagent-20260930-r19.md)；[证据与面试说明](researchagent-20260930-r19-evidence.md)。保留此前版本。

r19 在 r18 基础上补充宿主管理实验模型接口：实验代码生成请求 JSONL，宿主按预算调用模型并写响应，凭据不进入沙箱；请求/响应哈希、收据、失败和未知调用不重放均可查。保留 3 条宿主 measurement 导入 V3 的隔离回放证据、历史量化结果和失败边界；明确接口已实现但尚未形成新的科学结果，复杂课题稳定自主科研仍未完成验收。最新回归 444 后端/33 前端、接口针对性 49/49；SQLite 隔离迁移已通过。

# 2026-09-29 历史投递版本：r13

当前文案：[r13：研究决策与工具编排](researchagent-20260929-r13.md)；[证据与面试说明](researchagent-20260929-r13-evidence.md)。旧版保留。

Auto Research移至首项，明确研究LLM自主选择工具、Codex承担编码、宿主运行实验并回传指标。保留Harness、RAG、记忆、压缩、引用与策略的现有量化成果；SciFact 300条claim及基线/候选/消融用于说明真实基础流程的验证范围。完整方法迭代仍待验收。本版同步文案、[证据说明](researchagent-20260929-r13-evidence.md)和[目标差距](../auto-research-goal-gap-20260929.md)。最新原文简报经局部修订后11/11段落通过模型核验；独立方法迭代任务仍失败，只有1版方案且无有效测量，未据此新增研究成功率。后续修复完整后端432/432，前端33/33；真实方法迭代复验正在新隔离目录运行，尚无最终结果。当前r13原位更新，旧稿可由Git历史查阅；主服务尚未更新。

# 2026-09-29 历史投递版本：r12

当前文案：[r12：真实Auto Research基础流程](researchagent-20260929-r12.md)；[证据与面试说明](researchagent-20260929-r12-evidence.md)。旧版保留。

新增首个真实SciFact课题：研究LLM自主调研、委派Codex，在超限后调用独立测量工具，取得baseline/candidate/ablation宿主指标并交付报告。基础流程验收通过；根据科学结果多轮修订方法、完整V3总档案和较新Agent研究项目仍待完善。保留现有检索、记忆、Harness量化指标，不把该候选分类器的分数归因自研Agent。420/32完整回归及后续38项增量验证有独立记录；主服务尚未更新。

# 2026-09-29 历史投递版本：r11

当前文案：[r11：补充真实Harness修复收益](researchagent-20260929-r11.md)；[证据与实现范围](researchagent-20260929-r11-evidence.md)。r10及此前版本保留。

固定SciFact 8题×2配置，端到端通过31.25%→75.00%（5/16→12/16），多工具协议失败9/16→0/16；此为同任务/模型/预算下产品修复的前后复测，不以Codex编码成绩归因研究Agent，不替换旧全32题结果。416/416后端、32/32前端通过。新Auto Research r3已以blocked结束：2次真实CLI调用，两次均记录编码token超限且无有效实验测量，完整闭环未通过验收；主服务尚未加载本轮代码。目标、已实现部分及剩余三个交付见[目标差距](../auto-research-goal-gap-20260929.md)。

# 2026-09-29 历史投递版本：r10

当前文案：[r10：研究工作台与 Agent Harness](researchagent-20260929-r10.md)；[指标证据与面试说明](researchagent-20260929-r10-evidence.md)。r9及此前版本保留。

本版明确Codex是研究LLM的工具，已有检索、记忆、证据、恢复和策略指标保持原口径；工程测试数量移到证据说明。普通聊天、单次调研和可选Auto Research并存。控制器与工具编排已实现，完整真实课题仍在联调，不能写为已可靠完成自主科研。

当前后端409/409、前端32/32通过；SciFact第二次真实课题已以blocked结束，没有有效实验测量。两次原始Codex收据确认实际启动及超时，原汇总漏计问题附复核保留。固定八题DeepSeek复测尚未形成完整结论。主服务尚未加载本轮代码。详细状态见[目标差距](../auto-research-goal-gap-20260929.md)。

# 2026-09-29 历史投递版本：r9

当前文案：[r9：研究工作台与 Coding Agent 工具化](researchagent-20260929-r9.md)；[指标证据与实现范围](researchagent-20260929-r9-evidence.md)。r8 及更早版本保留。

r9 保留真实检索、记忆、压缩、恢复和策略指标，补充有效样本数；Codex 条目描述已接通的持久工具、方案版本、异步委派和实测反馈，不用排序代码成绩证明研究 Agent 贡献。Auto Research 完整真实课题仍在验收，尚未作为可靠自主科研成果。当前后端400/400、前端32/32通过；旧产品benchmark冻结版370/31与原始模型成绩不变。

研究控制器、工具入口与项目准备已接通当前工作区，主服务尚未部署本轮代码。真实原生环境准备通过，Luna路由7/7；SciFact首次课题已以blocked结束，未产生有效实验指标，完整验收尚未通过。当前状态与下一步见[目标差距](../auto-research-goal-gap-20260929.md)和执行验收（本地档案：`evals/reports/auto-research-foundation-20260929/report.md`）。

# 2026-09-29 历史投递版本：r8

当前文案：[r8：研究工作台、实测记忆与Coding Agent执行基础](researchagent-20260929-r8.md)；[指标证据与实现范围](researchagent-20260929-r8-evidence.md)。保留[r7](researchagent-20260929-r7.md)及此前全部版本。

r8补入历史真实记忆20/20、两次压缩事实/约束22/24、LongMemEval 25道保留题的历史会话Recall@5 8.33%→37.47%，以及查询规划候选Recall@5 64.92%→72.14%。恢复改写为复用已有成果、仅续跑核验；Codex条目描述注册实验中已经接通的执行、测量、最多3轮迭代、预算与恢复，移除用排序候选分数证明研究Agent贡献的写法。完整Auto Research与研究LLM自主调用Codex尚未接通，不提前写成成果。未新增真实模型成绩或改动原始结果；当前工作区381项后端回归另存执行基础报告（本地档案：`evals/reports/auto-research-foundation-20260929/report.md`）。

目标与当前差距见[说明](../auto-research-goal-gap-20260929.md)。新持久CodingTool已有实现和单元测试，但尚未接入工作台和研究LLM工具集，不能写成已交付的自主研究闭环。

# 2026-09-27 历史投递版本

当时文案：[r6：问答证据增强、失败恢复与V4有限迭代](researchagent-20260927-r6.md)。保留r5及此前版本；下方为历史存档。

r6新增实际问答原文重排与收据恢复、最多3轮Coding Agent实验、开发全局最佳保留、累计预算及早停。两组32道自编题共64次真实Luna配对：首组整体13/16对13/16，原文片段覆盖91.67%→100%、引用充分19/22→20/22；独立两新来源14/16对14/16、拒答均4/4。后者没有证明整体通过率提升，不把未交付与待核验稿的分项差异当作语义能力提升。

三条真实失败检查点分别只用1次核验恢复、零新增检索/整答生成/局部修订，旧成绩不覆盖。V4对照2轮无改善保留基线，原文候选1轮开发达标早停，9道同源可回答终验Recall@5 88.89%→100%；另列内容重排成本。最终358项后端、31项前端、V1 6/6、V2 5/5通过。

本轮总报告（本地档案：`evals/reports/evidence-qa-20260927/report.md`） · 独立来源结果（本地档案：`evals/reports/evidence-qa-20260927/independent-r1/review-summary/report.md`） · 源码与运行归因（本地档案：`evals/reports/evidence-qa-20260927/source-provenance.json`） · 回归记录（本地档案：`evals/reports/evidence-qa-20260927/regression.json`）。当前默认策略仍为原基线；简历可写已实现能力及小样本实测，不声称广泛泛化或完整成本节省。

# 2026-09-25 历史投递版本

当时文案：[r5：V4排序修复与原文重排](researchagent-20260925-r5.md)。保留此前全部版本；下方r4为历史记录。

r5保留既有查询规划、端到端问答及引用核验指标，新增有实测依据的V4检索改善：历史30题Recall@5 64.92%→94.84%、跨文档10题36.43%→92.86%，新同源事实终验9题88.89%→100%；同24份模型响应回放，接口解析回退9/24→0/24。191/191只表示原文引文一致性。新增结果不能替换旧完整问答指标，不能写成“答案正确率100%”。

证据：简要报告（本地档案：`evals/reports/v4-first-cycle-20260923/ranking-repair-20260925/report.md`）、指标（本地档案：`evals/reports/v4-first-cycle-20260923/ranking-repair-20260925/metrics.json`）、用量（本地档案：`evals/reports/v4-first-cycle-20260923/ranking-repair-20260925/usage.json`）、隔离审计（本地档案：`evals/reports/v4-first-cycle-20260923/ranking-repair-20260925/security-audit.json`）。实测模型gpt-5.6-luna；源码提交可由本文件本次git log定位，实测原始指纹与后续守卫修补分别归档。评测后发现并修复文档标签副本可读，已保存轨迹未见访问，保留限制不称严格盲测。默认普通问答策略保留。

# 简历版本与证据

历史投递文案：[2026-09-23 r4 版](researchagent-20260923-r4.md)。保留[首版](researchagent-20260923.md)、[r2：引用核验与恢复](researchagent-20260923-r2.md)、[r3：V4首个闭环](researchagent-20260923-r3.md)。

r4保留此前有效的检索、问答和核验数字，新增开发失败反馈、独立终验与283/29回归结果。第四次真实Luna尝试172.015秒交付候选，使用264134累计token，但开发Recall@5 90%→80%、测试64.92%→43.89%，未达标且未启用；前三次失败（两次超时、一次工具/输出预算终止）全部保留。该实测支撑工程闭环及退化拦截，不支撑新增算法收益。详见反馈报告（本地档案：`evals/reports/v4-first-cycle-20260923/feedback-improvement-20260923/report.md`）。

## 更新约定

项目实现、评测原始记录与简历表达分别管理。开发时检查真实问题、保留失败与完整条件；简历优先呈现已经实现、可以演示、有实测依据的成果，允许精简实验局限，不以达到论文级证明作为开始投递的前置条件。新结果更可靠、更有说服力时，更新投递文案，旧版及其证据通过 Git 保留。

可以写小样本上的观察提升，但保留规模和指标名称；不把检索召回写成答案准确率，不补造数值，不扩大语料规模，不把计划中的功能写成已实现。原报告不随简历措辞优化而改分，不用成功重跑覆盖失败。

每次替换指标需要记录：对应源码提交、数据/模型/预算、评测日期、原始输出位置、旧值/新值及变化原因。结果下降也保留；当前简历不必随每一次随机波动改写，更换依据应是版本或评测条件的实质更新。

2026-09-23 第1步增量版：[researchagent-20260923-r2.md](researchagent-20260923-r2.md)。首版保留；增量版保留原检索和端到端数字，新增三条列表旧失败稿0/3→3/3、负例3/3与恢复成果，并更新回归数量为256/27。依据为核验回放报告（本地档案：`evals/reports/harness-strategies-20260921/verification-recovery-20260923/report.md`）。这是相同草稿/原文的核验阶段对照，不是新端到端benchmark；恢复先注入中断再用真实模型续跑，发现补充断言引用不足后局部修订一次，主段落复用。对应源码为本次修复提交（可由本文件git log定位），模型gpt-5.6-luna、核验输入14080 token、12次请求；交付指纹与所有输出保存在报告目录。这是r2保存时点的历史记录；V4当前状态以r4及反馈报告为准。

## 历史版本数字出处

| 简历内容 | 依据 | 面试时能展开的范围 |
| --- | --- | --- |
| 60题、四组消融，Recall@5/10、MRR和送达率 | 控制集指标（本地档案：`evals/reports/harness-strategies-20260921/control-retrieval-metrics.json`）、选型记录（本地档案：`evals/reports/harness-strategies-20260921/selection.json`） | 20题开发选型，40题历史控制含30题可回答；不是新盲测 |
| 最终8题16次、5/8→6/8、152.9→131.4秒 | 最终指标（本地档案：`evals/reports/harness-strategies-20260921/release-live/live-metrics.json`）、逐题复核（本地档案：`evals/reports/harness-strategies-20260921/release-live/review-notes.json`） | 一项基线失败来自核验超时；成绩是系统整条任务的结果，不是纯检索因果收益；首轮也保留 |
| 78/78原文快照一致 | 原文审计（本地档案：`evals/reports/harness-strategies-20260921/release-live/trace-audit-metrics.json`） | 比较保存的原文内容及哈希；不等于78条事实都正确，也不等于通读全文 |
| 283后端、29前端 | V4反馈回归（本地档案：`evals/reports/v4-first-cycle-20260923/feedback-improvement-20260923/regression.json`） | 工程回归数量，不是模型回答正确率；含 UTF-8、重复测量和缓存身份回归 |
| 新增反馈编码172秒交付候选、退化门槛拦截 | V4反馈指标（本地档案：`evals/reports/v4-first-cycle-20260923/feedback-improvement-20260923/metrics.json`） | 单次编码耗时，非全部流程时间；候选退化，执行完成不等于优化成功 |
| 反馈、策略版本、启用门槛与回退 | [实现说明](../harness-strategies.md)、门槛结果（本地档案：`evals/reports/harness-strategies-20260921/release-live/gate-audit.json`） | 能力已实现；本候选因基线计量不完整而未启用，用户研究区仍用原基线 |

完整依据：V4反馈报告（本地档案：`evals/reports/v4-first-cycle-20260923/feedback-improvement-20260923/report.md`） · Harness报告（本地档案：`evals/reports/harness-strategies-20260921/report.md`）、V3报告（本地档案：`evals/reports/roadmap-v3-20260921/report.md`）。后续优先级：[下一轮迭代建议](../next-iteration.md)。

## 本地版本保存范围

首个 Git 版本保存现有源码、测试、依赖清单、公开评测数据、文档、简历以及当前 Harness/V3 可读验收产物。`.env`、虚拟环境、用户数据库、Chroma 索引、运行缓存和历史源码副本留在本机，不加入 Git；`.env.example` 只保留配置样例。此前大量旧评测继续原地保留，文档中的历史链接在本机仍可查阅；新克隆不会带回被忽略的旧报告。数据库备份继续使用既有 `data/backups/`，Git 不替代数据备份。


本地研究档案不随源码上传；公开累计对照与逐题结果见[评测报告](../research/rag-comparison-20261011.md)，归档范围见[发布说明](../github-publication.md)。
