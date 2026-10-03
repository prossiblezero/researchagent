# ResearchAgent：检索、上下文、记忆与会话架构调研

调研日期：2026-09-18。目标项目：`D:\project\researchagent`。状态：调研与建议，尚未批准或实施产品改造。配套交接见 [实施方案](implementation-plan.md)，来源版本与哈希见 [sources.json](sources.json)。

> 选型修订：用户澄清 BPE 指 BGE，并建议 ChromaDB / pgvector。当前推荐已更新为 BGE-M3 + 本地持久化 ChromaDB，SQLite 保存业务与证据；原 E5-small + 精确扫描作为对照。见 [修订说明](retrieval-revision.md)。

## 推荐方向

保留现有 Python 显式研究循环、SQLite、证据引用和单机后台任务，演进为“按需检索 + 可回查的上下文压缩 + 分层记忆 + 可恢复会话”。RAG 已经存在于当前本地问答路径，只是检索较弱且被隔离在固定 LOCAL_QA 流程里。优先把检索与原文读取接入研究工具循环，再做混合召回、会话检查点和跨会话记忆。

不要把所有问题都强制送进 RAG，也不要把可靠性完全寄托于模型自觉。模型选择查询、资料与何时继续检索；程序保证研究区范围、用户指定的来源限制、引用有效性、预算、记忆来源和会话顺序。

压缩、记忆、缓存是三件事：压缩缩小当前请求，记忆支持未来找回，供应商缓存复用相同输入前缀。每轮改写摘要虽然能缩短输入，也可能增加模型调用、破坏缓存并累积摘要错误。

## 调研方法与证据边界

本次定位并保存了 43 份相关仓库源码/文档快照，针对所述机制读取了关键函数及文档段落；不是对整个仓库的审计或部署测评。下面的 GitHub 链接固定到提交，不能把文件名或 README 宣传当作功能实测。官方网页按访问日期记录。没有安装、运行这些 Agent，也没有新增付费模型评测。

| 项目 | 核对提交 | 证据层级 |
|---|---|---|
| pi（原 badlogic/pi-mono，现 earendil-works/pi） | `46c9de402bddf46b03c3b9f46487b777aaa41861` | 源码 + 仓库文档 |
| openai/codex | `7498521d288b9b3b96ffba4eedf089d8d6e06a84` | 源码 + 官方文档 |
| openclaw/openclaw | `2a5bb456231b0aa51950f873935fac9954d8e32c` | 源码 + 仓库文档 |
| NousResearch/hermes-agent | `d177b119e9c56c9ddc0b7379ffce52341ec06584` | 源码 |
| anomalyco/opencode | `b02acc1e30ef55f7f181fec8d2f241d26f022683` | 源码 |
| anthropics/claude-code | `31a3b00bef145a0393d9dbf840a98674fec07712` | 公开仓库边界核对；能力依据官方文档 |

用户明确 ZCode 指智谱产品，并同意未开源则跳过。本次未确认其公开核心实现，故不作内部架构比较。检索发现的非官方 zcode-cli 不能代替 ZCode 的源码证据。

Claude Code 的公开仓库包含插件、文档入口等，并不等于完整核心运行时开源；本次读取的 [许可证](https://github.com/anthropics/claude-code/blob/31a3b00bef145a0393d9dbf840a98674fec07712/LICENSE.md#L1) 为保留所有权利、受商业条款约束。关于自动记忆与压缩的结论来自官方文档，不冒充源码逆向结论。

## 各项目真正值得借鉴的机制

| 项目 | 核对到的设计 | 对本项目的借鉴 | 不直接照搬的部分 |
|---|---|---|---|
| pi | 结构化摘要、保留近期尾部、记录压缩事件、会话分支 | 摘要可替换当前窗口，原始事件仍可回看；探索不同研究方向可以 fork | TypeScript 运行时与整套树状存储后端 |
| Codex | 每线程记忆提取与后续整合分开；记忆可指向原始 rollout | “候选记忆 → 去重/更新 → 小索引 + 按需回查”；记忆不是当前事实证明 | 整套 Rust 服务、全局调度器和供应商特定协议 |
| OpenClaw | memory_search / memory_get；混合搜索；来源分级与记忆整合门控 | 检索工具与精确读取分离；程序检查来源、范围及摘要质量 | 多通道网关、复杂 dreaming 调度和多 Agent 管理 |
| Hermes | 固定核心记忆快照；SQLite 会话搜索；旧工具清理后再摘要 | 缓存友好的记忆快照；少量核心偏好自动注入，大量历史按需检索 | 可插拔外部记忆提供商系统与庞大兼容层 |
| OpenCode | 按保留窗口清理旧工具结果；压缩与会话 fork | 先处理最占空间的工具输出，保留最新工作；分支不覆盖原会话 | 整套 Effect/TypeScript 架构和其面向大窗口的绝对阈值 |
| Claude Code | 持久规则与自动记忆分开；小记忆索引、主题文件按需加载；先清工具输出再摘要 | 让用户能查看、修正记忆；小目录自动带入，详情需要时再读取 | 私有内部实现、不能核实的“零 RAG”等宣传性推断 |

### pi：保存压缩事件，保留可继续工作的尾部

[prepareCompaction 源码](https://github.com/earendil-works/pi/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/agent/src/harness/compaction/compaction.ts#L634)读取上次摘要与 retainedTail，再按近期 token 预算寻找边界；过大的单轮可拆出 turn prefix 另做摘要。工具结果不能任意与调用分离。[文档](https://github.com/earendil-works/pi/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/docs/compaction.md#L27)解释主动阈值和手工 compact。[fork 实现](https://github.com/earendil-works/pi/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/agent/src/harness/session/fork.ts#L28)通过条目及父子关系选出分支的逻辑状态。

值得借鉴的是保留“为什么、做到了哪里、接下来做什么”，而不只是摘录几段网页。当前 pi 文档仍能看到旧 firstKeptEntryId 表述，而所读底层实现使用 retainedTail；本报告以具体提交源码为准，不照抄旧字段。

### Codex：提取与整合分开，记忆按需读回

[Phase 1](https://github.com/openai/codex/blob/7498521d288b9b3b96ffba4eedf089d8d6e06a84/codex-rs/memories/write/src/phase1.rs#L51)有候选任务领取、提取结果和 token 统计；[Phase 2](https://github.com/openai/codex/blob/7498521d288b9b3b96ffba4eedf089d8d6e06a84/codex-rs/memories/write/src/phase2.rs#L47)另行做整合、版本与工作区校验。这表明长期记忆不应等同于每次对话结尾随手写一段总结。

[记忆使用模板](https://github.com/openai/codex/blob/7498521d288b9b3b96ffba4eedf089d8d6e06a84/codex-rs/ext/memories/templates/memories/read_path_v2.md#L1)明确：摘要提供历史背景，只有原记录中的证据、时序或不确定性会影响答案时才继续读；记忆不能证明当前行为。[压缩提示](https://github.com/openai/codex/blob/7498521d288b9b3b96ffba4eedf089d8d6e06a84/codex-rs/prompts/templates/compact/prompt.md#L1)保留进度、关键决定、约束、未完成事项与关键引用。可以借鉴这些机制，但本项目不需要复制它的后台整合 Agent 系统。

OpenAI [官方 Compaction 文档](https://developers.openai.com/api/docs/guides/compaction)还提供 Responses 原生压缩，返回不透明压缩项。当前项目用 DeepSeek / Sudocode 的 Chat Completions 兼容接口，未验证其支持该能力。近期采用应用层可读检查点，避免把会话可恢复性绑定到某一家隐藏状态。

### OpenClaw：LLM 选择检索，程序维护检索合同

[memory_search / memory_get 的真实工具定义](https://github.com/openclaw/openclaw/blob/2a5bb456231b0aa51950f873935fac9954d8e32c/extensions/memory-core/src/memory-tool-contract.ts#L88)分别返回检索命中与指定范围原文，并对过去的工作、决定、偏好等设定召回要求。它不是“把所有记忆每轮拼进去”，也不是“模型可以随意跳过所有来源检查”。

[混合排序实现](https://github.com/openclaw/openclaw/blob/2a5bb456231b0aa51950f873935fac9954d8e32c/extensions/memory-core/src/memory/hybrid.ts#L68)结合向量和关键词信号，并有时间、重要性与多样性处理。对 ResearchAgent 应先采用可解释的关键词 + 向量召回与去重；论文事实不能一概按时间衰减，旧论文可能仍是正确的一手来源。

[记忆写入设计](https://github.com/openclaw/openclaw/blob/2a5bb456231b0aa51950f873935fac9954d8e32c/docs/concepts/memory-architecture.md#L127)区分临时记录和长期整合，检查内容来源。[压缩保护](https://github.com/openclaw/openclaw/blob/2a5bb456231b0aa51950f873935fac9954d8e32c/docs/concepts/compaction.md#L29)要求压缩后的实际输出通过检查，失败保留原历史。[工具清理文档](https://github.com/openclaw/openclaw/blob/2a5bb456231b0aa51950f873935fac9954d8e32c/docs/concepts/session-pruning.md#L9)强调投影稳定与原始历史保留，也明确清理会从最早被修改的位置影响缓存。因此不能把“尽量多压缩”当成唯一优化目标。

### Hermes：固定记忆前缀，历史提供搜索和定位

[Memory Tool](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/tools/memory_tool.py#L1)和 [MemoryStore](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/tools/memory_tool_store.py#L68)明确将核心记忆在会话加载时冻结为快照，之后的写入不改动已发给模型的前缀。[微压缩代码](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/agent/micro_compaction.py#L1)说明逐轮滚动摘要默认关闭，原因就是前缀反复重写。

[session_search](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/tools/session_search_tool.py#L1)支持检索、定位某条消息附近、读会话和浏览，不额外调用 LLM，返回数据库真实消息。[压缩流程](https://github.com/NousResearch/hermes-agent/blob/d177b119e9c56c9ddc0b7379ffce52341ec06584/agent/context_compressor.py#L4754)先清理旧工具内容，再保护头尾并摘要中间部分。

本项目可以借鉴“小快照 + 原文检索”，但不能为了缓存继续服从过时偏好。当前用户纠正、删除记忆和研究区权限变化必须立即生效，必要时主动重建上下文。

### OpenCode：先减工具结果，再做语义摘要

[prune 实现](https://github.com/anomalyco/opencode/blob/b02acc1e30ef55f7f181fec8d2f241d26f022683/packages/opencode/src/session/compaction.ts#L270)从后往前保留近期工作，跳过未完成工具和受保护工具，标记较早结果为 compacted；[fork 实现](https://github.com/anomalyco/opencode/blob/b02acc1e30ef55f7f181fec8d2f241d26f022683/packages/opencode/src/session/session.ts#L691)另建会话分支。绝对阈值服务于其运行环境，不适合直接复制到本项目 16K/32K/64K 的应用预算。

### Claude Code：用小目录组织跨会话记忆

[官方 Memory 文档](https://code.claude.com/docs/en/memory)区分人工维护的 CLAUDE.md 与自动记忆。当前文档规定 MEMORY.md 起始 200 行或 25KB（取先达到者）在会话开始载入，详细信息放主题文件按需读取。文档也提醒这属于上下文，不是能强制限制工具的权限配置。

[官方运行机制](https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up)说明先清理较旧工具输出，必要时摘要对话，并承认早期详细指令可能丢失。不能因此认为摘要天然无损，也不能推断其内部所有检索算法。

## 当前 ResearchAgent 的关键缺口

| 位置 | 现状 | 会导致什么 |
|---|---|---|
| library.py:400–423 | 最近 100 份资料仅按标题/类型先选最多 8 份，再按展开词计数选段 | 未入选文件的正文永远不可召回；题名没有术语时容易漏检 |
| workbench.py:84、192–205 | Router 看 20 条/12,000 字符；本地任务只传最近 6 条的截断上下文 | 长追问指代、较早约束和未完成研究计划会消失 |
| contracts.py:10、policy.py:11、models.py:170 | 系统规则/策略限定 search、read；工具参数只保留 query/url | 不能只加一个 retrieve 函数就宣称接通 RAG；协议链路要一起改 |
| context.py:207、loop.py:146 | 最近两对工具 + 词项摘录；claims 传空 | 有限预算下缺少任务目标、证据缺口和冲突的语义工作状态 |
| verify.py:14 | SUPPORTED 依赖词项重合与粗略否定检测 | 不能把这个状态直接升级为长期“已验证事实” |
| experience.py:8、storage.py:72 | 固定模板经验被保存，没有跨任务提炼召回 | 有执行记录，不代表具备研究经验记忆 |
| workbench_store.py:239–270 | 重启后标为 interrupted；重试重新排队 | 不是从已完成工具结果的检查点继续 |
| loop.py:141、models.py:142 | 开头动态进度每轮变化；缓存 usage 细项被丢弃 | 可复用前缀短，实际模型缓存收益不可测 |

上述为代码核查。本次没有把过去的 “Failed to fetch” 或 Meta-Harness 漏检全部归咎于某一个问题；具体失败仍需相应任务 trace 才能定因。

已有正式评测补充：122 次研究循环上下文构建有 18 次裁剪；真实工作台工具复用 0/50；253 次 Agent 模型请求中 250 次有基础 usage、0 次保存缓存细项。后两类缓存不能混为一个指标。详见既有 [核查记录](../../../evals/reports/agent-formal-20260918/context-memory-cache-audit.md)。

## 是否需要 RAG，怎样让 LLM 使用它

需要检索增强，但重点是研究材料与可靠证据，而不是向量数据库本身。

| 方案 | 优点 | 缺点 | 选择 |
|---|---|---|---|
| 每轮固定检索并塞入片段 | 容易实现、流程一致 | 闲聊也检索；难以追问、换查询、读邻段，噪声与成本上升 | 不采用为统一流程 |
| LLM 自主调用现有词频检索 | 改动最少 | 仍受标题预筛选、词面不一致影响 | 只适合作为第一步过渡 |
| LLM 工具调用 + 程序管理范围/证据 + 混合召回 | 按需取证、可多跳、能回查、可量化 | 要补全工具合同、索引与评测 | 推荐方向 |

检索工具负责取证，不在内部再启动一个不可见的“搜索—生成答案 Agent”。建议以 retrieve 返回来源、位置、短摘录与候选 ID，以 read_evidence 读取原文邻段/完整章节；由主研究循环统一判断是否足够、是否换查询或联网补查。

显式“只依据这份论文”时只允许这份材料；“本地有什么”要查资料库；“我们上次决定了什么”查历史与记忆；“你好”不检索；“核实最新结果”可先查已有材料再联网。需要限定来源时由后端应用约束，不能只靠工具描述中的一句提示。

第一步以现有 SQLite FTS5 + 实体规范化 + 模型查询展开替代词频扫描；第二步加入本地多语向量、用排序融合而非把词频和余弦原始分数相加。用户明确术语、论文 ID、年份时保留精确匹配。文档默认没有“只查最近 100 份”的上限。

本机现有 Python 3.12.14 / SQLite 3.53.4 已实测支持 unicode61 与 trigram FTS5。trigram 对不足 3 个字符的查询有局限；中文需额外生成汉字二元索引文本和保留短实体检索。初版轻量对照模型为 [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3)，固定该 revision；已核对 384 维、512 长度与 query:/passage: 前缀要求。没有下载或测速该模型，不能承诺召回提升或 CPU 延迟。

## 压缩、记忆和会话应该各自负责什么

1. **原始历史**：持久保存用户消息、模型输出、工具请求/结果、模型与用量；是回查与恢复依据。
2. **会话工作记忆**：当前目标、明确约束、研究计划、候选、证据缺口、冲突、下一步。随同一会话延续，结束后成为可检索记录。
3. **上下文检查点**：历史某个边界的摘要、原始事件范围与近期尾部，服务继续运行。不是新增事实来源。
4. **跨会话记忆**：按研究区管理有来源的事实、确认偏好和决策，带版本、状态和来源。不同研究区默认隔离。
5. **资料库**：论文/代码/文档的原始材料及索引，不能和用户偏好混为一张任意文本集合。

新会话清空当前任务状态，但共享同一研究区的资料与允许使用的长期记忆；恢复会话从检查点和未压缩尾部继续；分支会话继承选定时点前的状态，不把分支之后的结论混回父会话；归档只改变会话可见状态，不代表删除研究资料。

压缩优先级：限制新工具结果体积 → 对已完成的旧大结果使用稳定的短投影和原文引用 → 接近预算时生成结构化语义检查点 → 校验后原子提交。保留最新用户要求、未完成事项、精确数字/ID、冲突和证据位置。失败保留旧检查点；反复摘要无进展时停止重试并给出可恢复状态。

## 缓存与质量要一起衡量

[OpenAI 官方缓存文档](https://developers.openai.com/api/docs/guides/prompt-caching)支持固定可复用前缀、追加历史并观察真实 usage 的做法。但 Sudocode 的返回字段、缓存策略、计费可能不同，不能照搬官方价格或声称自动享有相同行为。

运行期固定规则、工具定义和小型记忆快照；历史追加；进度放新消息/新工具结果的运行状态里；会话摘要只在真正触发压缩时更新。不要为了缓存预填无用 token，也不把每轮变化的检索片段都移到第一条系统提示。稳定材料块仅在同一批材料反复提问且确实会重用时前置。

缓存指标至少包括：输入 token 命中率、发生命中的请求占比、usage 字段覆盖率、写缓存 token、实际费用、P50/P95 延迟。工具结果、文档去重和模型缓存分别统计。压缩与记忆生成产生的模型费用也必须进入总账。

## 验证依据与不直接引入的复杂度

已有 72 次正式开发评测继续保留为基线，不修改原分数。新增检索 Recall@k、证据覆盖、引用语义支持、压缩后的约束/待办保留、跨会话更新/遗忘、恢复后重复动作等测试。长期记忆维度借鉴 [LongMemEval](https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc) 的信息提取、多会话推理、知识更新、时间推理和拒答；本次仅研究方法，没有运行其数据集，不报告公开榜单成绩。

不增加 LangGraph、Redis、分布式队列、独立向量服务、知识图谱或自动多 Agent 团队。现有任务规模下，这些都不是解决目前漏检和失忆的必要条件。本次后续修订加入嵌入式 Chroma 索引组件，仍不增加独立向量服务。完整阶段、字段、工具合同、迁移、验收与回滚见 [实施方案](implementation-plan.md)。
