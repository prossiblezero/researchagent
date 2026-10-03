# V4 排序修复调研记录（2026-09-25）

本记录是方案依据，不是新算法实测。只读核对了现有代码、归档候选与公开实现；未下载模型、修改生产检索或调用真实模型。历史测试题已被多次观察，只可称历史回归，不能称新盲测。

## 问题与判断

现有冻结实验使用 FTS5 与 BGE-M3 父片段候选，RRF 的 k=60。原候选在词法无命中时，把同源片段数量及编号距离作为加分，开发 Recall@5 已由 90% 降到 80%，仍交付最后版本。排名候选池有证据不等于排在前列；编号相邻也不等于回答问题。

开发失败 `semantic-07`、`semantic-08` 的词法匹配数均为 0，目标片段 dense 名次分别为 6、17。此时只调整 sparse/dense 权重或对 dense 分数做单调变换，无法改变 dense 的相对顺序。应把“问题＋原文内容”的相关性重排作为独立对照，不能期望纯融合调参解决全部中文语义错误。

## 三种候选及验证顺序

1. **归一化融合，作为低成本消融。** 保留 RRF 基线，另测每题内归一化的 lexical/dense 分数线性融合；不直接把余弦相似度加到 RRF 上。只用开发集选择少量预声明权重，并记录各题型表现。Pyserini 同时实现 RRF、线性插值和 min-max 归一化，支持把它们作为不同候选比较，而非凭经验叠加不同比例的分数。它对“无词法命中且 dense 排错”能力有限。
2. **按原文内容重排，作为主要质量对照。** 对固定 top20–40 候选，用既有模型 API 做一次 query＋passage 的列表重排，或在已有依赖支持时使用多语言 CrossEncoder。输入只含查询、片段编号及原文，不含 gold/答案；输出只能引用输入编号，拒绝重复、越界与无效响应，失败回到基线。若预计算并缓存信号，应计入模型耗时、token、失败与缓存命中；缓存重放不能冒充多次真实模型稳定性。CrossEncoder 需另外模型文件，不是现有 BGE-M3 embedding 的另一调用方式；英文 MS MARCO MiniLM 示例不能直接代表中文质量。
3. **MMR／来源覆盖，作为可关闭消融。** MMR 优先相关性，再惩罚已选内容的冗余。不要强制 top5 各来源至少一条，或把来源数量当质量：单文档精确题可能需要多个同源片段。仅在查询明确要求跨文档时尝试来源覆盖；通用路径优先内容相似度、最小相关性门槛及较高相关性权重。当前冻结信号没有文档间向量相似度，真正 cosine MMR 需宿主补充无标签特征。生产路径已有每来源最多三条的送达限制，实验排名与线上送达指标需分开。

先完成宿主最佳版本保留与退步回退，再做上述独立消融，锁定候选后终验。每个版本保留代码、输入、开发测量和选择原因；以基线初始化最佳版本，全部退步时正常输出“未找到改进”。至少记录 Recall@5、MRR、零召回题数、分题型表现、候选池召回上限、真实新增延迟与调用量。选优可靠性与排序质量收益是两个指标。

## 可查阅出处

| 出处 | 核对到的机制与本项目用途 |
| --- | --- |
| [Pyserini fusion，提交 8237181](https://github.com/castorini/pyserini/blob/8237181239312494b2acaf514856598098c9923d/pyserini/fusion/_base.py) | `reciprocal_rank_fusion` 默认 k=60；`interpolation` 为 alpha 线性融合；`normalize` 先 min-max 到 [0,1]。该实现明确引用 Cormack 等的 **SIGIR 2009**（CCF A）论文 *Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods*，[DOI](https://doi.org/10.1145/1571941.1572114)。ACM 页面本次有安全验证，论文内容依据实现注释而非声称读过全文。 |
| [Sentence Transformers CrossEncoder 文档](https://www.sbert.net/examples/cross_encoder/applications/README.html#combining-bi-and-cross-encoders) | 明确先用 Bi-Encoder 召回，再对每个 query/hit 联合打分。模型得分是相关性信号，最终指标仍由宿主标签计算。 |
| [Haystack LLMRanker，提交 6df20e1](https://github.com/deepset-ai/haystack/blob/6df20e123675063dd8120c52ff1d3eb96b9d888c/haystack/components/rankers/llm_ranker.py) | 提示包含 query 与 document content，返回 JSON 编号；生成或解析失败可返回原输入顺序。这里只借鉴合同与降级方式，不直接搬入框架。 |
| [LangChain MMR，提交 a063ec2](https://github.com/langchain-ai/langchain/blob/a063ec26dd21678069d3b3d44770dffcff2b60f0/libs/core/langchain_core/vectorstores/utils.py) | 贪心目标为 lambda×query similarity − (1−lambda)×与已选项最大相似度。依赖 NumPy，机制可在现有环境中小规模实现，无需引入 LangChain。 |
| [ColBERT 官方实现](https://github.com/stanford-futuredata/ColBERT)／[SIGIR 2020 论文](https://arxiv.org/abs/2004.12832) | **SIGIR 2020**（CCF A）*ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT*；token-level 表示＋MaxSim 为细粒度内容匹配提供依据。但需新表示与索引，本轮不据此重做检索栈。 |
| [DSPy MIPROv2，提交 da1f087](https://github.com/stanfordnlp/dspy/blob/da1f0871ec8f34e913ecde7c5ebab473022b9c63/dspy/teleprompt/mipro_optimizer_v2.py) | 先评估 default program，以其初始化 best score/program；完整验证分数严格更好才替换，支持基线保留。 |
| [OpenEvolve，提交 38207bb](https://github.com/algorithmicsuperintelligence/openevolve/blob/38207bb6e6eeb2c8cd2689ee61e48bf403a372ab/openevolve/database.py) | 独立维护 `best_program_id`；新程序只有在 `_is_better` 时更新最佳，避免最后一个版本覆盖最佳。无需引入其岛屿或 MAP-Elites 架构。 |

所有收益都是待验证假设。本记录不报告未正式复现的排名数字，也不把其他项目的成绩作为本项目提升。
