# 普通 RAG 与 Agentic RAG：固定24题对照

截至2026-10-11，Agent按“原先完成18题＋专项修复恢复2题”累计完成 **20/24（83.3%）**，与普通RAG的 **20/24（83.3%）** 并列展示。Agent首次运行仍为 **18/24（75.0%）**；累计记录复用了旧成绩和两条检查点恢复，**不是修复后最终版本的整组重跑，也不是同条件新对照**。

普通RAG为10-10的24题实测，Agent首次为10-09记录，专项恢复在10-11完成。原始答案、评分和指标文件保持不变；本次新增累计列及逐题复算（本地档案：`research-notes/verification-format-recovery-20261011/cumulative-metrics.json`），恢复过程见核验报告（本地档案：`research-notes/verification-format-recovery-20261011/report.md`）。

|任务范围|单次 API＋Prompt|普通 RAG 首次|Agent 首次|Agent 累计完成记录|
|---|---:|---:|---:|---:|
|全部24题|12/24（50.0%）|20/24（83.3%）|18/24（75.0%）|20/24（83.3%）|
|QASPER|3/6（50.0%）|3/6（50.0%）|3/6（50.0%）|3/6（50.0%）|
|SciFact|2/6（33.3%）|6/6（100.0%）|6/6（100.0%）|6/6（100.0%）|
|HotpotQA|6/6（100.0%）|6/6（100.0%）|5/6（83.3%）|6/6（100.0%）|
|项目公开资料问答|1/6（16.7%）|5/6（83.3%）|4/6（66.7%）|5/6（83.3%）|
|资料超过单次上下文的14题|5/14（35.7%）|12/14（85.7%）|12/14（85.7%）|13/14（92.9%）|
|完整资料可放入的10题|7/10（70.0%）|8/10（80.0%）|6/10（60.0%）|7/10（70.0%）|

按累计记录逐题比较：双方各有2题独有通过；首次运行时为Agent独有2题、普通RAG独有4题。ReAct恢复属于超长资料组，VA-174恢复属于完整资料组。子集有包含关系，不能相加。

## 固定条件

- 使用与既有对照相同的24题及语料、deepseek-v4-1-flash、temperature=0、单次16,384上下文及严格评分标准。普通RAG与API＋Prompt使用相同生成提示；Agent保留其执行与核验提示。
- 普通RAG使用冻结Agent的FTS5/BM25、BGE-M3、RRF及已准备的本地交叉编码器重排；以原问题检索一次，按排序取8个去重后的完整父段落，受相同14,080估算输入预算约束。直接取完整段落，避免把工具的短预览当作RAG正文。
- 普通RAG一次生成，不改写查询、不追加检索、不使用模型工具或核验反馈修订。流式传输沿用已验证客户端，保留固定网络重试、原始输入、全部响应与用量。
- 输入和协议在首次答案生成前冻结。评分用参考答案和实际引用原文；参考答案及gold ID不进入生成或检索。
- 成功完成要求答案正确、完整、引用充分、无实质性无据添加且正常交付；Agent沿用产品核验交付标准。外部评分不反馈给普通RAG。

## 耗时与模型用量

|方法|平均耗时（秒）|任务模型请求|已知任务token|未知用量请求|
|---|---:|---:|---:|---:|
|单次 API＋Prompt（固定原文前缀）|9.17|24|191,198|0|
|普通 RAG（单次混合检索 Top-8）|12.20|24|90,686|0|
|Agentic RAG（历史同题结果）|151.61|238|1,828,540|3|

上表仅为首次运行成本。两题恢复另增加4次真实模型请求、28,996个token，分别耗时30.084秒和39.860秒；不把这些局部续跑时长当作最终版本端到端成本。普通RAG耗时包含预热后检索与答案生成，初始化和外部评分另计；Agent包含工具、核验与产品后处理。两边总调用量不同，这不是等算力对照。

## 逐题结果

|任务|API＋Prompt|普通 RAG|Agent 首次|Agent 累计记录|
|---|---|---|---|---|
|qasper-ca5a82b54cb707c9b947aa8445aac51ea218b23a|通过|未通过|通过|通过|
|scifact-536|未通过|通过|通过|通过|
|hotpotqa-5abc6c2655429959677d6aac|通过|通过|未通过|通过（专项恢复）|
|project-independent-ind-normal-03|未通过|通过|通过|通过|
|qasper-9b97805a0c093df405391a85e4d3ab447671c86a|通过|通过|通过|通过|
|scifact-837|未通过|通过|通过|通过|
|hotpotqa-5a78d2b955429974737f78c0|通过|通过|通过|通过|
|project-evidence-normal-02|未通过|通过|未通过|通过（专项恢复）|
|qasper-518dae6f936882152c162058895db4eca815e649|未通过|未通过|未通过|未通过|
|scifact-516|未通过|通过|通过|通过|
|hotpotqa-5ab576dc5542997d4ad1f13d|通过|通过|通过|通过|
|project-independent-ind-cross-02|未通过|未通过|通过|通过|
|qasper-84bad9a821917cb96584cf5383c6d2a035358d7c|通过|通过|通过|通过|
|scifact-1088|未通过|通过|通过|通过|
|hotpotqa-5a86399e5542994775f60733|通过|通过|通过|通过|
|project-evidence-cross-03|未通过|通过|未通过|未通过|
|qasper-c598028815066089cc1e131b96d6966d2610467a|未通过|未通过|未通过|未通过|
|scifact-1199|通过|通过|通过|通过|
|hotpotqa-5ac3d667554299076e296ca5|通过|通过|通过|通过|
|project-independent-ind-false-04|未通过|通过|通过|通过|
|qasper-902b3123aec0f3a39319ffa9d05ab8e08a2eb567|未通过|通过|未通过|未通过|
|scifact-502|通过|通过|通过|通过|
|hotpotqa-5abba3b1554299642a094aed|通过|通过|通过|通过|
|project-independent-ind-absent-01|通过|通过|通过|通过|

## 两种流程各自胜出的题目

Agent独有通过的两题说明继续检索和阅读能够补齐证据。两条核验交付故障随后恢复；Docling＋Hermes与词嵌入题的未完成结果仍保留，累计总分未超越普通RAG。

|任务|普通RAG表现|Agent表现与恢复|
|---|---|---|
|IPA数据标注（QASPER）|Top-8给出下一动作标签，但漏掉NER实体标注和完整结构化对话记录|分页阅读后补齐实体信息、对话记录和动作三元组|
|MemorySaver＋LlamaIndex跨文档问答|只回答重启后检查点丢失，未检索到查询截断代码|分别检索并阅读两份文档，回答查询切片限制|
|VA-174基地所在县|两跳证据齐全并交付Duval County|首次草稿正确但核验字段异常未交付；专项恢复后补首段引用并通过|
|ReAct search/lookup API|完整回答前5句、相似实体建议与下一匹配句|首次草稿正确但核验结构异常未交付；专项恢复后原答案不变并通过|
|Docling＋Hermes跨文档问答|同时回答页面迭代器、defrag不移动游标、失败保留旧摘要|误判Docling文本截断，漏答摘要失败回退，且未交付|
|词嵌入算法（QASPER无答案控制）|明确无法从资料确定唯一算法，保留通过|仅限定缺少算法原理/训练细节，仍直接作答，按原复核失败|

词嵌入题存在原始标签与多算法文本之间的语义边界。本轮原文复核保留自动评分；不因累计结果变化重新调整该题标签；保留普通RAG通过、Agent未通过的原复核结果。

对照原始答案：IPA普通RAG（本地档案：`research-notes/ordinary-rag-24-20261010/runs/qasper-ca5a82b54cb707c9b947aa8445aac51ea218b23a/answer.md`）、IPA Agent（本地档案：`research-notes/subset-e2e-20261009/runs/qasper-ca5a82b54cb707c9b947aa8445aac51ea218b23a/after/answer.md`）、MemorySaver＋LlamaIndex普通RAG（本地档案：`research-notes/ordinary-rag-24-20261010/runs/project-independent-ind-cross-02/answer.md`）、MemorySaver＋LlamaIndex Agent（本地档案：`research-notes/subset-e2e-20261009/runs/project-independent-ind-cross-02/after/answer.md`）、历史交付失败与拒答复核（本地档案：`research-notes/subset-e2e-20261009/adjudications.json`）。

## 简历与项目判断

原简历中相对API＋Prompt的数值仍成立，但不能据此声称多轮Agent编排优于普通RAG。普通RAG对照复用了项目的混合检索层，这组成绩同时验证了该检索层的价值。Agent补证据的两道案例可以展示实际能力；是否采用普通RAG优先、缺证据时升级Agent的策略，需要另行实现并在冻结题集验证，本轮没有修改产品策略。

## 解释范围

这是一个固定Top-8配置和24题小样本的系统对照。Agent使用10-09历史结果，普通RAG在10-10运行；传输方式、服务负载及模型后端漂移未控制，不能将差值全部归因于某一个Agent环节。SciFact仅6题，包含在14题超长资料组中，不是官方完整榜单成绩。

普通RAG首次评测没有重跑挑最好结果或按答案调节Top-K。新增累计列只加入两条已知故障的专项恢复，未经全量回归，不能给累计列计算新的整体平均耗时、token或统计显著性。同模型辅助评分和实施代理原文复核不等于独立专家盲评。

冻结协议（本地档案：`research-notes/ordinary-rag-24-20261010/protocol.json`） · 输入审计（本地档案：`research-notes/ordinary-rag-24-20261010/input-audit.json`） · 逐题指标（本地档案：`research-notes/ordinary-rag-24-20261010/metrics.json`） · 原文复核（本地档案：`research-notes/ordinary-rag-24-20261010/source-review.json`） · 运行脚本（本地档案：`research-notes/ordinary-rag-24-20261010/run.py`）


## 数据版本与公开范围

公开仓库保留本报告、逐题结果与[优化历程](../optimization-history/12-agentic-rag-and-verification-recovery.md)。原始请求、全文证据和运行快照遵循[发布范围](../github-publication.md)留在本地；下列SHA-256对应本次复算的真实文件。

|本地文件|SHA-256|
|---|---|
|`research-notes/ordinary-rag-24-20261010/metrics.json`|`03b5c6f889f4e737fa0fe2e236bb6a4a3c0c708c57976b749f9a557aeba08431`|
|`research-notes/verification-format-recovery-20261011/metrics.json`|`c6ac3c31f36c09e224d9231fe2f89c8662f8d4cd2874077a00f19d9d2140571b`|
|`research-notes/verification-format-recovery-20261011/source-review.json`|`ca0f38c730f6242f10303d287fcf7fa2a9bdf66e2c4288ec6e8f1f769abad32c`|
|`research-notes/verification-format-recovery-20261011/cumulative-metrics.json`|`8e159b1d559d0923c63d51eb6e4bda8d2c8cae19ff801dc2fd75809a7483c654`|


本地研究档案不随源码上传；公开累计对照与逐题结果见[评测报告](rag-comparison-20261011.md)，归档范围见[发布说明](../github-publication.md)。
