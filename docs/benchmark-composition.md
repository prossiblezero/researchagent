# ResearchAgent 混合评测选型（2026-09-28）

状态：2026-09-29 修正评测入口。9月28日的手写规则对比没有使用产品能力，不能支持简历中的产品提升指标。当前冻结320条任务，以生产检索、完整问答和工程回归分别报告；运行记录见 evals/reports/product-benchmark-20260929。

## 能力与数据的对应关系

| 顺序 | 开源任务 | 项目能力 | 接入范围与指标 |
|---|---|---|---|
| 1 | QASPER | 论文全文问答、漏读、答案与证据段落绑定、无答案拒答 | 保留原始论文、question_id、原题、多参考答案及 evidence；本轮统计生产段落检索及选定8题的真实问答语义/引用/阅读覆盖；官方 Answer F1、Evidence F1 尚未接入。固定 80 题（40 dev + 40 held-out），按答案类型分层。 |
| 1 | SciFact | 检索科学证据，判断结论得到支持、被反驳或缺少证据 | 保留 claim_id、原始断言、文档及句子级 rationale；本轮报告生产检索Recall@5/MRR@5，选定8题做真实核验问答；官方句子级/联合指标尚未接入。固定 60 条（30 dev + 30 held-out）有公开标签的断言；检索使用完整发布语料，不能只放 gold 文档。 |
| 2 | HotpotQA distractor | 跨文档检索和多跳证据组合 | 每题保留全部10篇上下文段落及 supporting facts，包含 bridge/comparison；保留官方答案与supporting facts；本轮报告生产检索与选定8题语义/引用评分，非官方joint指标。固定 50 题（25 dev + 25 held-out）；只能称 distractor 子集，不能称 fullwiki 成绩。 |
| 2 | LongMemEval cleaned | 多独立会话中的事实回忆、信息更新、时间推理、拒答 | 保留时间、角色、session_id、原始历史；固定使用 cleaned S 的 50 题（25 dev + 25 held-out）覆盖主要类别，保留完整干扰会话。本轮只执行真实跨会话历史检索，完整答案评估尚未运行；不把oracle版本当真实检索成绩。 |

上表数量已经抽出并冻结。四个公开轨道合计 240 条，项目真实场景再加 80 条，总计 320 条唯一任务。QASPER 和 SciFact 先形成可独立运行的论文/证据轨道；之后增加跨文档和记忆轨道。每条轨道保留独立版本、报告和运行状态，不等待全部四项都接入才使用。

## 项目真实场景

复用已有两组16题问答和已保存失败作为场景回归的起点，明确这些是“按产品场景自建题”，不是自然采集的用户流量。进一步从用户实际研究操作中固化案例：论文/项目资料导入后的追问、中文跨论文比较、原文未读完导致的错误、引用绑定失败、核验超时恢复、研究区资料共享、多会话隔离、历史分支、报告版本和受控实验。

用户自然任务与工程故障注入单列。每个案例保存任务来源、输入状态、所需证据、预期行为、失败轨迹和脱敏依据；不复制私有会话到公开数据集。V4用上述检索开发任务做算法迭代，终验任务不提供给编码代理；当前注册排序实验与SWE-bench仓库修复并非同一能力，暂不为了扩充清单接入SWE-bench。

## 抽样、运行与评分约束

- 原始下载统一到 D:/paper/researchagent-benchmarks；datasets 下保存可分发的固定子集、来源/许可、上游版本、原始ID、哈希和抽样清单。各数据集单独遵守上游许可，不继承项目H1的CC0声明。
- 优先保留上游train作为开发池、带公开标签的validation/dev作为终验池。论文型任务按paper_id去重；记忆型任务按完整问题历史隔离。已用来调参的样本降为历史回归，不能继续宣称未见终验。
- 抽样以固定种子和原始ID哈希排序，先按题型分层，再运行模型。不得根据成功率删题；缺失字段、文本不可表示的图表及其他排除项全部记入清单。QASPER第一批若仅接入文本题，明确报告为文本子集。
- 给系统的输入只包含问题、许可的来源与上下文；gold答案、rationale、supporting facts及LongMemEval的has_answer/answer_session_ids仅供宿主评分，不能进入模型输入、记忆、检索排序特征或V4工作目录。
- 保留原始问题语言。中文翻译作为独立扰动条件，不能直接替换官方原题后仍标为原版子集。官方答案指标与项目引用/原文阅读指标分开，避免答案措辞影响引用统计。
- 同一固定模型、预算、源数据做 baseline/candidate 配对；真实模型优先沿用用户已授权的 Luna。先做离线数据/评分验证，再做少量真实联调，正式运行保留全部首次失败、Trace、请求、usage覆盖及源码指纹。
- 各轨道分别报告，不把Recall、Answer F1、工作流成功率强行平均成一个总分。项目结果应称“基于公开数据的固定子集评测”，不冒充完整官方榜单成绩。

## 已核对的官方来源

- QASPER：[官方基线与Answer/Evidence F1说明](https://github.com/allenai/qasper-led-baseline)、[数据发布与字段说明](https://huggingface.co/datasets/allenai/qasper)。数据卡说明有5,049个问题、1,585篇NLP论文，提供多参考答案、段落证据和无答案标注。
- SciFact：[官方仓库](https://github.com/allenai/scifact)、[数据格式](https://github.com/allenai/scifact/blob/master/doc/data.md)。train/dev包含标签、test标签隐藏；必须保留原始文档和句子级证据语义。
- HotpotQA：[官方站点](https://hotpotqa.github.io/)。distractor协议提供10篇上下文段落和支持事实，与fullwiki区分；数据按CC BY-SA 4.0发布。
- LongMemEval：[官方仓库](https://github.com/xiaowu0162/LongMemEval)、[官方cleaned数据](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)。官方更新建议使用cleaned历史，含500个问题；S与oracle历史条件不同。

HumanEval、tau-bench、Lost in the Middle继续保留为指标/设计参考。它们不计入已接入任务数量；AgentBench和SWE-bench也不因出现在参考清单而算已使用。

## 2026-09-29 产品调用协议

原始下载保留在 `D:/paper/researchagent-benchmarks/raw`。`datasets/open/selection.json` 固定原有公开题目ID，修正证据时不按成绩换题。QASPER仅精确空白归一化匹配正文、摘要、图表说明；无文本映射的证据保留并单列，不用相似度猜gold。SciFact保留完整5183文档语料和各rationale备选组；HotpotQA保留10篇上下文及原句；LongMemEval使用题目局部会话ID，输入模型不暴露answer session ID。项目语料恢复完整8来源，所有项目题标为历史/工程回归。

- 296条检索任务两臂直接调用 `Retriever.retrieve`，baseline为生产lexical，candidate为生产hybrid（FTS5+BGE-M3+RRF）。各自生产分段也属于此配置消融的一部分。评分用实际返回top5、2000token预览预算；无答案和无法精确映射题不计召回，空检索不能算正确拒答。
- 24条工程任务绑定 `tests/` 真实test method，实际创建SQLite并执行会话隔离、历史分支、记忆、报告/实验版本、引用、压缩恢复等产品行为。不混入LLM准确率。
- 32题完整问答在输出出现前冻结（先Luna，服务持续异常后按用户授权新建DeepSeek配对队列，模型结果分开）：QASPER、SciFact、HotpotQA、项目各8题；公开部分每组4dev+4held_out，项目按4类各2题。同一队列的两臂均使用hybrid和同一个模型/quick预算，candidate增加reading guide和原文证据重排。实际执行 `Workbench.execute / LOCAL_QA`，包括原文读取、核验、报告和记忆后处理；路由预先指定，不声称测量路由能力。
- 每题每臂使用独立数据库，只复用不变的公开来源向量。gold只由宿主和后续盲评读取，不进入生成链。独立模型调用隐藏策略名评阅答案语义及引用，属于模型评分，不是独立人工评审。
- 首轮失败及在途状态保留，恢复不自动重新付费执行。读取覆盖按实际回答请求中的原文窗口计量；保存全文、重排读过全文、核验器看到全文，都不算回答模型通读。

复现入口（使用已有本机BGE-M3）：

```powershell
.venv-v3\Scripts\python.exe -X utf8 evals/run_mixed_benchmark.py --stage all --output evals/reports/my-product-run
```

`--stage` 可选freeze、retrieval、workflow、qa、judge、report；`--dataset` 可重复选择轨道。已有output只跳过已记录首次尝试，源码/数据指纹不一致会拒绝混跑。正式数据和失败不为正向指标重新抽样。


2026-09-29运行补充：本机RTX4080配合独立CUDA PyTorch加速BGE索引，旧CPU虚拟环境不被覆盖；wheel存放D:/paper/researchagent-benchmarks/runtime-wheels。正式文档检索在product-benchmark-20260929/gpu-retrieval，历史检索以paired-history为准（同一来源SQLite复制两臂，消除随机会话ID的同分排序影响）；原独立ID版本保留。Luna因持续服务过载/流中断停在10条运行收据，86个请求中34条异常，1条响应未完成；DeepSeek在deepseek-qa下完成同32题的新配对。最终状态以父目录report.md/metrics.json为准，不合并两种模型的成绩。
