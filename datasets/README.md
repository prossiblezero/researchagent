当前并行评测：[真实最终结果](../evals/reports/parallel-research-final-audit-20261003/report.md)，8次父运行/16次子研究已完成，严格合同6/8、总体未提速。A-MEM与并行均已停止，本轮不再追加采样。

# 最新完成情况（2026-10-03）

A-MEM/LoCoMo开发与独立留出各40题的基线、MMR、Top10三组测量均已完成；共2080轮完整对话，原始预测与宿主分数独立复算一致。开发集MMR的Recall@10有提升，独立留出未达到冻结的R10/F1条件，完整结果及旧失败见[留出终验](../evals/reports/amem-holdout-final-audit-20261003/report.md)。下方日期更早的“未完成”仅为历史记录，不表示当前状态。

新增[并行调研面板](project/parallel-research-v1.json)：复用四道项目问题和公开论文/源码原文，两个独立问题一组，2组×2次重复×单/双worker，共16个真实子研究。用于检验调度效率及原文/引用质量，不并入原320题，也不是官方benchmark成绩。实际运行记录在`evals/reports/parallel-research-live-20261003-luna-r1/`；指标以其完成报告为准。

# ResearchAgent 数据集目录

这里是当前项目所有**活动评测输入**的唯一入口。运行脚本、实验模块和测试都从 `datasets/` 读取；`evals/reports/` 只保存运行结果，`evals/archives/` 只保存历史源码/快照。

## 先说结论

截至 2026-09-29，本项目固定了 320 条混合任务：QASPER 80、SciFact 60、HotpotQA distractor 50、LongMemEval cleaned S 50，以及项目场景 80（56 条历史问答/检索回归 + 24 条实际可执行工程回归）。活动评测调用生产 Retriever 和 Workbench；32 题另做真实模型完整问答配对（Luna 服务异常后，按授权用 DeepSeek 建立独立队列），运行状态以 `evals/reports/product-benchmark-20260929` 为准。不是官方 leaderboard 成绩。AgentBench、τ-bench、SWE-bench 仍只作参考。

9月28日旧报告只比较手写文本规则，未调用产品能力，不能用于产品提升或简历数字。其工作流检查只是字符串非空，不能视为工程通过率。旧报告保留并勘误。

四个公开子集保留原先选中的题目ID；QASPER纠正为精确证据映射，未映射证据单列且不参与召回均值。LongMemEval保留每题完整角色/日期/消息及干扰会话，导入真实独立会话进行 workspace history 检索；本轮没有其完整答案准确率。项目题已被历史实验使用，全部标为回归，不能称未见测试集。

`manifest.json` 对每个文件记录了来源类别、用途、切分、数量和 SHA-256；`open_source_benchmark_registry` 区分已经运行固定子集的轨道和仍只做研究参考的 benchmark。

2026-09-30 新增独立的 A-MEM/LoCoMo 研究面板，尚未运行，不并入上述 320 题或已有简历成绩：`open/locomo/` 保存四个完整官方 conversation（2,080 turns）与 80 道原题，按 conversation 分成 40 题开发集和 40 题保留集。`tasks.json` 与 `corpus.json` 不含 QA 答案，`labels.json` 仅供宿主评分。保留一个证据映射不完整案例，79 题可计算证据召回；全部 80 题保留答案评分资格。来源提交、确定性抽样规则和 CC-BY-NC-4.0 许可见 `selection.json` 和 `LICENSE.txt`。重建命令为 `python evals/prepare_locomo_case.py`，读取 D:/paper 中已固定的官方源码包。

## 目录

- `harness/`：Stage 1–7 与离线 Harness 行为回归，自建任务。
- `h1/`：H1 grounded QA 开发/测试切分。题目和 rubric 自建，外部 URL 只作为核验来源。
- `live/`：历史真实检索验收任务，自建题目。
- `research/`：研究问答任务及其公开论文/资料快照。
- `retrieval/`：V3 检索题、V4 反馈标签、gold 证据和审计。
- `evidence_qa/`：V3/V4 证据问答及独立官方来源转移面板。
- `open/`：QASPER、SciFact、HotpotQA distractor、LongMemEval cleaned S 的固定公开子集、语料、来源摘要和许可证元数据。
- `project/`：混合基准中的80条项目场景，以及独立的`auto-research-functional-v1.json`功能验收集（7类意图、原生依赖环境、真实SciFact研究课题）。另有`auto-research-reading-v1.json`，复测r4中实际失败的原文调研子任务。新增`auto-research-iteration-v1.json`检查实测反馈后的方案修订和同评分器再实验。这些功能验收不并入320题检索/问答成绩，不作为Codex能力或论文效果分数。
- `project/sequential-model-v1.json`：使用 LoCoMo 开发对话原文的连续宿主模型接口验收，检查第二次请求使用第一次真实回答；不读取 QA 标签，不并入 80 题记忆效果评测。

- `project/auto-research-amem-v1/`：固定 A-MEM 官方 robust 源码、原论文和独立运行环境的开发协议；完整保留 conv-26/conv-30 共 788 turns、40 道原题，39 题证据可评分。入口 `python evals/run_amem_acceptance.py --output evals/reports/<new-run> --model sudocode-luna`。标签仅由宿主评分，数据/源码哈希和 seed=[13] 固定；候选与消融完成后再冻结方法并启用独立保留集。2026-10-01完整开发基线已有效：788轮/40题，Answer F1 47.56%、Evidence Recall@5 39.62%，归档复算一致；候选/消融运行中，尚无方法收益或holdout结果，见[完整基线报告](../evals/reports/amem-baseline-checkpoint-20261001/report.md)。早期原生结果回收失败详见 [准备报告](../evals/reports/amem-environment-20260930/report.md)。

## 许可和可复现性

自建题目、gold、拒答规则和元数据沿用 [`DATA_LICENSE.md`](DATA_LICENSE.md) 的 CC0 说明；外部原文和来源链接仍受原作者许可约束。不要把来源快照重新发布成项目自有内容。每个冻结数据文件的内容哈希见 `manifest.json`。

## 活动测试

活动测试代码统一位于根目录 `tests/`。`evals/archives/` 内的 `tests/` 是不可变历史快照，保留原路径以便复现过去的运行，不属于当前测试入口。


2026-10-01 新增 `project/amem-reading-recovery-v1.json`：来自A-MEM真实任务的失败原文简报，固定问题、来源任务和原稿。用已有检查点测试核验/局部修订，不读取LoCoMo标签，不作为完整Auto Research或记忆质量指标。DeepSeek本次独立恢复已因预算耗尽失败，首轮10/13段通过，报告在 `evals/reports/amem-reading-recovery-20261001-deepseek-r1/`。用户更新Luna凭据后，完整A-MEM原任务已恢复并委派适配器修复，324条记忆检查点保留；目前仍无完整baseline成绩。


2026-10-01独立续跑：`project/auto-research-amem-continuation-v1/`复用完整40题/788turns开发协议与原始数据哈希，冻结完整提示基线，显式导入旧阶段419条记忆（人工审核迁移，0预测/0成绩）。模型、seed、数据、源码和参数绑定缓存条件；旧成本与新续跑成本分列。该阶段尚未完成，不能计入简历方法收益。


2026-10-01 输出目录恢复：`project/auto-research-amem-continuation-v2/` 保留相同 40 题/788 turns，人工诊断旧阶段将整个状态目录冻结的问题。新工作区只冻结协议指定的输入快照，重新生成全部预测，预算仅继承旧阶段剩余额度；旧失败和 20 题预测单独保留，不计为新增 benchmark 或完整分数。


2026-10-02 最新：r3因两次供应商overloaded结束，outcome=blocked/passed=false；完整baseline40题已由宿主重算通过，candidate仅20题部分预测，ablation与holdout未完成。新增 `project/auto-research-amem-continuation-v3/`：同一40题/788轮开发集，核对并继承baseline788轮及candidate756轮记忆，0旧预测/成绩导入；使用旧阶段剩余预算。协议与来源SHA见新case及 [迁移记录](../evals/reports/amem-continuation-preparation-20261002/cache-migration-audit.json)。这是同一基准的继续执行，不增加benchmark题量。

2026-10-02 同记忆开发案例：`project/auto-research-amem-fixed-memory-v1/` 复用完整788轮baseline记忆，所有方法读取相同JSON/向量，在原40道开发题重新生成预测；不增加benchmark题数，不导入旧答案。由研究模型取得新宿主反馈后决定方法与Codex委派。准备报告见 [固定记忆案例](../evals/reports/amem-fixed-memory-preparation-20261002/report.md)。此前r4三组已完成但存在记忆混杂且没有新方法迭代；独立holdout仍未评测。

2026-10-02 MMR修正：`project/auto-research-amem-mmr-correction-v1/` 明确承认r5之后的人工源码诊断：MMR应对全部已选项取max，原代码误取第一项。提供无标签的手算反例，沿用r5剩余预算及相同40题/788轮，要求Codex数值回归后新测量。不是新增benchmark题量，也不把代码修复当科研创新。见 [准备记录](../evals/reports/amem-mmr-correction-preparation-20261002/report.md)。

2026-10-02 修正后测量恢复：`project/auto-research-amem-corrected-measurement-v1/` 使用同40题/788轮记忆，冻结已通过数值检查的MMR修正，不引入旧预测。原r6因结果路径和SSL故障未完成，失败保留；新阶段只测量/解读，0编码，非新的方法发现或benchmark题量。

2026-10-02 独立留出：`project/auto-research-amem-holdout-v1/` 从既有open/locomo划分原样导出conv-41/42共40题、1292轮，不新增题量。开发r7完整比较后固定正确MMR与官方baseline；终验空缓存构建同一共享记忆，标签宿主私有，0编码、不调参，全部指标与失败公开留档。
