---
layout: guide
title: ResearchAgent：从一次回答到可追溯的研究流程
description: 从论文调研任务出发，读懂 Agentic RAG、原文证据、记忆、任务恢复与自动实验的设计和取舍。
---

<a id="top"></a>

# ResearchAgent：从一次回答到可追溯的研究流程

**项目学习手记 · 2026-10-11**

项目地址：[prossiblezero/researchagent][repo]。本文按问题展开，解释一个研究任务怎样经过检索、阅读、核验与恢复，最后成为可以检查的交付。适合先顺着读一遍，再对照源码拆开看。

代码说明对应公开仓库的 [7e3a3be 快照][snapshot]；实验引用各自报告中的历史测量，不是本文编写时重新跑出的成绩。文章借鉴了 [YYHDBL 的项目手记][reference]从动机、具体例子讲到设计取舍和不足的组织方式，内容与实现均围绕 ResearchAgent。

如果只有半小时，先读第一至四节、第七节和第十一节；准备动手时，再看第十三节。逐文件练习仍放在[分章源码教程][tutorial]中。

<a id="motivation"></a>

## 一、为什么要把论文问答做成一个研究工作台？

先看一个很具体的需求：

> 我已经读到一篇相关论文。能否顺着其中一位作者找到他的其他工作，判断是否属于同一个团队，再比较这些论文的方法、引用关系和演进？每个判断最好能回到原文。

这个需求看上去像一道问答题，实际包含了好几件不同的事。

首先要找到资料。已知一篇论文，并不代表已经拿到了作者的全部论文；搜到同名作者，也不代表找对了人。随后要读资料：标题和摘要能说明主题，却未必包含作者机构、发表版本、方法细节和参考文献。最后才是把几份材料放在一起，给出有依据的比较。

还有一个容易被忽略的问题：任务可能会中断。论文下载失败，模型响应格式不对，已经读过的证据在上下文压缩后丢失，都会让一个“理论上能回答”的任务没有交付。

ResearchAgent 处理的就是这样一段过程。它是一个面向论文和技术研究的本地工作台：用户围绕研究区保存资料和会话，Agent 按需要搜索、读原文、查资料库、组织证据，并在回答后核验。进一步需要实现和实验时，再进入 Auto Research。

这里值得学习的工程问题，是如何让模型始终拿到合适的任务状态，以及如何检查它执行到了哪里。模型 API 提供理解、决策与生成能力；项目实现工具接口、资料管理、执行约束、证据关联、恢复和实验反馈。

### 1.1 和“把论文贴给模型”相比，多做了什么？

如果相关原文很短，而且你已经找齐材料，一次 API＋Prompt 完全可能回答得很好。这个项目的价值主要出现在材料还没找齐、整个资料库放不进一次上下文、或者回答需要继续补证据的时候。

以资料库里的几千篇摘要为例，用户通常不会先替模型挑好正确的几篇。系统需要把“大量可访问材料”转成“当前问题需要的证据”，再组织答案。后面介绍的检索、原文回读、核验和记忆，都在服务这件事。

这也决定了评估方式：既要看检索有没有找对材料，也要看最终回答是否完整、有支持，并且真的交给了用户。只看生成文字是否流畅，很难定位系统的价值和问题。

<a id="task"></a>

## 二、先把一次研究任务拆开

用团队论文调研作贯穿例子。下面是**教学流程**，不代表每次运行都固定执行这些动作，也不作为额外实验结果。

| 阶段 | 此时需要回答的问题 | 系统留下什么 |
| --- | --- | --- |
| 确定种子 | 用户指定的是哪篇论文、哪位作者？ | 论文身份、DOI、原文署名位置 |
| 找候选 | 这个作者还发表了哪些相关工作？ | 作者候选 ID、作品列表、版本与公开链接 |
| 获取原文 | 哪些已下载，哪些仍不可读？ | 文件、解析块、失败记录、下一可读位置 |
| 核实身份 | 同名作者是否有充分证据对应？ | ORCID、姓名与邮箱、机构和合作者线索 |
| 比较关系 | 谁引用谁？方法相同在哪里、不同在哪里？ | 定向关系线索、各论文实际已读证据 |
| 组织答案 | 是否回答了所有子问题？ | 带证据引用的草稿 |
| 核验交付 | 引用能支持本段吗，有没有漏项？ | 核验结果、有限修订、最终状态与报告 |

这条链上，每一步都有不同的失败方式。发现了四篇候选，但只读到两篇全文，应该保留“还有两篇待获取”；不能把候选摘要当成四篇全文核验完成。两篇论文共同引用了第三篇，也不能因此写成它们互相引用。

从代码看，主路径可以压缩成下面这张图：

```text
用户消息
   ↓
Workbench：保存消息、判断意图、创建任务
   ├─ CHAT：普通对话
   ├─ LOCAL_QA：基于可见本地资料问答
   ├─ RESEARCH：搜索与读取外部资料
   └─ AUTO_RESEARCH：研究、方案、编码和实验的外层循环
                     ↓
ResearchAgent：模型决策 → 工具执行 → 观察结果 → 再决策
                     ↓
来源与证据 → 草稿 → 核验 / 补读 / 修订 → 报告
                     ↕
SQLite、执行检查点、Trace 与用量记录
```

初读源码时不必先看完整目录。沿 [server.py][src-server] → [workbench.py][src-workbench] → [loop.py][src-loop] 追一次请求，就能找到入口、任务调度和实际研究循环。想弄清信息保存在哪里，再打开 [workbench_store.py][src-store]。

<a id="loop"></a>

## 三、Agent 的核心，是把观察结果送回下一次决策

一次普通模型调用，输入之后就等答案。研究任务的下一步却经常要在工具返回后才知道：搜索结果里有几篇同名论文，需要打开核对；读取的片段提到了另一个方法，需要继续检索；答案核验发现漏了一个要求，需要补读。

所以 `ResearchAgent.run()` 维护一个有界循环。模型可以提议工具调用，也可以提出最终回答；宿主负责执行、记录和检查，再决定是否继续。

下面的**教学伪代码**只保留骨架，省略流式输出、恢复、工具批次和异常处理：

```python
while within_budget:
    context = build_context(question, observations, evidence, progress)
    decision = model.complete(context, allowed_tools)

    if decision.is_tool_call:
        validate_arguments_and_scope(decision)
        observation = execute_tool(decision)
        save_observation_and_checkpoint(observation)
        continue

    verdict = check_answer(decision.answer, evidence)
    if verdict.needs_repair and repair_budget_remains:
        add_targeted_feedback(verdict)
        continue
    return deliver_answer_with_status(verdict)
```

学习这段循环时，可以一直追问：**模型提出了什么，程序真正允许并完成了什么？** 二者之间就是 Harness 的工作。

### 3.1 工具 schema 只是合同的入口

模型通过工具 schema 看到工具名、说明和参数。返回的内容先转成内部 `ModelDecision`，然后再经过参数、作用域和预算检查。

比如，JSON 格式合法不代表 URL 可以访问；模型写了一个 E 编号，也不代表证据目录里存在这个编号。工具返回的网页文字属于外部数据，不能把其中的指令当作用户授权。

模型适配读 [models.py][src-models]，内部数据结构读 [contracts.py][src-contracts]，执行限制读 [policy.py][src-policy]。这三个位置能帮助你把“提示词约束”和“宿主硬检查”分开。

### 3.2 为什么预算不能只有一个 max_steps？

有的任务一直重复同一次检索，有的工具很慢，有的模型没有调用工具却反复生成。一个步数上限很难同时解释这些情况。

当前循环区分工具尝试、模型轮次、输入预算、输出预留和时间限制。工作台 quick / standard / deep 档的工具上限分别是 **8 / 16 / 24**，配置上下文预算分别是 **16,384 / 32,768 / 65,536**；CLI 的默认工具预算则是 2。它们是不同入口的默认值，不要混在一起。

这些数字也不是模型服务商保证的实际窗口。程序估算、供应商 tokenizer、模型接口上限之间仍可能存在差异。达到预算后，系统需要明确终止或交付有依据的部分，不能靠无限重试保证完成。

源码入口：[ResearchAgent.run()][src-loop]、[预算常量][src-store]。进一步练习：[Agent Loop 与工具合同][chapter-loop]。

<a id="rag"></a>

## 四、为什么说这是 Agentic RAG？

先把三个概念拆开，很多讨论就清楚了。

| 概念 | 发生了什么 | 团队论文中的例子 |
| --- | --- | --- |
| 多条检索 | 对多个子问题发出查询，查询之间可以没有依赖 | 分别查论文 A 和 B 的实验设置 |
| 多跳检索 | 下一次查什么依赖上一跳得到的信息 | 从种子 DOI 找作者 ID，再找该作者作品 |
| 多跳推理 | 联合多份证据得出结论，未必需要多次检索 | 结合两篇原文判断它们是否使用同一评测协议 |

一次检索如果已经召回全部证据，普通 RAG 也可能完成多跳推理。反过来，执行了很多次搜索，并不意味着形成了正确的推理链。

### 4.1 本项目的“Agentic”具体在哪里？

这里把普通 RAG 基线定义为：以原问题检索一次，把选出的段落放入 prompt，再生成一次答案。它的材料集合在生成前已经确定。

ResearchAgent 把 `retrieve` 和 `read_evidence` 暴露成模型可选工具。模型能够根据当前观察重新发出查询、限定论文、打开原文窗口，或者在核验后补读。**下一步的信息获取方式，可以根据上一步结果改变。** 这是本项目称为 Agentic RAG 的直接依据。

工作台还区分聊天、本地问答和联网调研。本地问答在能力层关闭外部工具，联网调研才可以搜索外部来源；检索能力和联网权限是两件不同的事。

不过，当前实现没有一个经过验证的“普通 RAG 先答、失败才升级 Agent”的自适应成本策略。也不能把 Agent 自己生成下一条查询，表述成默认启用了另一个独立查询规划器；可选策略与默认行为要分别看配置。

### 4.2 一个真实案例：答案写出来以后，为什么还需要回读？

现有实验中有一道跨文档问题，同时问：LangGraph 的 MemorySaver 在重启后是否保留检查点，以及 LlamaIndex 如何限制生成过多的查询。

Agent 分别检索持久化指南和 QueryFusionRetriever 源码，阅读两份材料后成稿。核验发现答案额外声称“资料未提供其他限制机制”，这句话缺少支持，于是 Agent 按 offset 补读代码并修订。最终回答包括 RAM 检查点随进程丢失，以及 `queries[: self.num_queries - 1]` 与原始查询的数量关系。

这里的关键不只是“调用了两次检索”，而是补证据和修订由实际缺口驱动。执行过程见[价值与案例说明][value-report]；它证明这条路径执行过，不能单独估计补读模块贡献了多少百分点。

<a id="retrieval"></a>

## 五、底层检索：先找到位置，再打开原文

Agent 能决定何时检索，但检索器本身仍要把材料找准。否则模型只是更灵活地使用一个不可靠的工具。

### 5.1 从 PDF 到索引，中间保存了什么？

资料导入由 [library.py][src-library] 处理，保存文件身份、元数据和可定位的原始块。PDF 的页码、文本块，网页或代码的章节、行号，都是后面追溯答案的基础。

检索层再基于原文建立索引。原始文档块和向量编码窗口是两个对象：为了编码长度限制，长块会进一步切窗口；召回短窗口后，阅读仍要回到父块和相邻原文。

这能解释一个常见疑惑：为什么已经 `retrieve` 过了，还要 `read_evidence`？因为检索返回的是候选位置和短预览，原文可能有被预览省略的限定、否定或者关键代码。

### 5.2 BM25、BGE-M3 和 RRF 分别负责什么？

| 组成 | 擅长的情况 | 容易出现的缺口 |
| --- | --- | --- |
| SQLite FTS5 / BM25 | 精确术语、函数名、论文缩写 | 问法与原文不同，关键词没有重合 |
| BGE-M3 dense embedding | 同义表达、语义接近的段落 | 主题接近，但并没有所问事实 |
| RRF 排名融合 | 合并两个通道的候选排名 | 排得靠前仍不代表证据充分 |

BM25 分数和向量距离不能直接当作同一尺度相加。当前实现按候选在各列表中的排名计算：

```python
rrf_score[ref] += 1 / (60 + rank)  # rank 从 1 开始
```

比如 A 在关键词列表排第 1，在向量列表排第 5，合计约 `0.03178`；B 只在一个列表排第 1，约 `0.01639`。这个融合办法利用排名，鼓励两个通道都命中的候选，但依然需要实验判断效果。

本项目使用 BGE-M3 的 dense embedding；词法检索由 FTS5 提供。模型名字里有 M3，不代表项目同时启用了其全部稀疏和多向量能力。

检索路径还支持可选重排或证据选择。这些能力要按具体运行配置解释，不能把所有实验策略都写成默认链路。阅读 [Retriever.retrieve()][src-retrieval] 时，先抓住基础混合检索，再看可选分支。

### 5.3 为什么向量命中后还要“回表”？

向量库是可重建的索引。用户删除一份资料、修改一个原文块后，旧向量可能暂时还存在。若系统直接信任向量命中，就可能把过期内容再次交给模型。

所以结果会检查 SQLite 中的当前记录和内容哈希，阅读时还会重新校验作用域与原文。相似度再高，也不能绕过当前资料是否可见、是否仍然有效的判断。

检索的 `corpus` 区分 documents、memory、history；`scope` 区分当前范围、研究区与跨会话范围。检索什么内容、可以跨到哪里，都必须保留在返回结果与权限检查中。

### 5.4 找到了原文，也不等于整篇读完

`read_evidence` 可以返回窗口、偏移和后续位置，也支持沿文档读取。已读窗口保留自己的证据身份与原文位置；后续窗口需要继续打开。

因此，“没有在当前窗口看到”“没有在候选池里找到”和“整篇论文没有提到”，是三个不同强度的结论。系统提示和核验都需要保护这个边界，否则一条检索漏召回就会变成错误的否定结论。

向量权重尚未准备或后端不可用时，系统保留词法检索，并记录降级原因。先运行 `setup_retrieval.py` 才能准备本地权重；请求过程中不会偷偷下载模型。更细的流程见[混合检索教程][chapter-retrieval]。

<a id="relations"></a>

## 六、论文的“社交网络”：先把身份和边的含义说清楚

当问题变成“这个作者还写过什么，这些论文之间有什么关系”，文本相似度只是线索之一。需要明确表示作者、作品和引用方向。

### 6.1 同名为什么不能直接合并？

以 Liang Zhao 为例，仅有相同英文姓名不足以证明是同一个人。论文首页列了多个机构，也不能推出每个作者都属于每个机构，更不能直接推导师生关系或团队正式成员身份。

当前作者识别优先读取首页的清晰署名，并保留署名顺序和位置。同名记录仍然是不同论文上的独立署名；消歧结果区分共同 ORCID、冲突 ORCID、同全名且邮箱相同，以及待核实同名。

机构和共同作者会作为补充线索返回。没有足够身份依据时，结果保持 `unresolved_same_name`。对扫描件、复杂排版或姓名缩写，系统也不会凭一个首字母自动补出全名。

这是一套带证据约束的作者消歧规则，还有解析和覆盖范围的限制，不是已经解决了全部学术实体对齐问题。

### 6.2 从已知论文出发，怎样找到其他作品？

联网工具 `discover_author_papers` 从一个已保存、可读的种子论文出发：

1. 确认指定作者全名出现在种子原文署名中，种子有可用 DOI。
2. 用 DOI 定位 OpenAlex 作品记录，并校验返回 DOI 一致。
3. 在该作品的署名中找到作者 ID 候选；有歧义时继续保留候选。
4. 依据作者 ID 和可选主题词分页查其他作品。
5. 沿返回的公开地址读候选原文，核对身份、版本和主题。

这个起点比全网按名字找人多了一层约束。但 OpenAlex 的作者聚合也不是身份真值，返回的作品列表仍是候选，不会自动成为已核验关系，也不会自动导入资料库。

工具只在允许联网、且当前研究区有可读论文的任务中提供。`LOCAL_QA` 不能因为问题提到了作者就自行联网。

### 6.3 本地引用关系怎么查询？

`lookup_papers` 提供以下入口：

| mode | 得到的线索 | 使用时要注意 |
| --- | --- | --- |
| `authors` | 论文署名与定位 | 首页机构列表不等于作者逐一归属 |
| `author_papers` | 同全名作品与身份线索 | 同名不自动归并 |
| `references` | 指定论文引用了哪些已保存论文 | A 引用 B 是有方向的 |
| `cited_by` | 哪些已保存论文引用了指定论文 | 范围仍受本地库限制 |
| `shared_references` | 指定论文共同引用的已保存论文 | 共同引用不等于相互引用 |

引用索引在参考文献区域匹配 DOI 或完整题名，保存方向与原文位置。没有入库的论文、识别失败的参考文献，不会出现在完整边集中，所以**索引没有一条边，不能证明论文没有引用**。

一次实际任务中，系统找到了 DLPMAC 与 KNNDA 都引用 CAPIMAC，继续回读两侧参考文献和 CAPIMAC 首页，核对 Liang Zhao 为第二作者兼通讯作者。这个案例说明为什么署名顺序、引用方向和原文回读需要一起保留。实验来源见[团队论文案例][value-report]。

### 6.4 到这里，为什么还没有直接上完整 GraphRAG？

GraphRAG 是一类利用显式图结构帮助检索和组织上下文的方法。图遍历可以支持沿关系找材料；社区检测和社区摘要可以帮助回答跨大量文档的主题、群体和全局归纳问题。具体能力取决于实现，不能把 GraphRAG 和“保证多跳推理正确”画等号。

ResearchAgent 当前已有作者与引用关系查询，也能多轮检索和联合证据作答；尚未实现完整的全库图抽取、社区划分和社区摘要检索。SQLite 中存在实体或边的表，也不等于完整 GraphRAG。

下一步是否引入，应看失败集中在哪里。如果经常漏掉已知关系邻居，先检查身份与引用边；如果材料已找到但回答漏项，先检查证据组织和答案修订；如果大量问题要求跨几百篇论文总结研究群体和主题结构，社区摘要才有更明确的评估对象。

现在没有同题 GraphRAG 对照，不能从团队十题的结果推出它一定没有价值。当前选择是先补齐可解释的作者／引用关系检索，再用实际任务判断更完整图能力的收益。

代码集中在 [paper_relations.py][src-relations]，参数与边界见[作者与引用关系说明][relations-doc]。

<a id="evidence"></a>

## 七、回答“有引用”，为什么还不够？

最容易制造可信感的回答，是在每段后面加一个引用编号。但编号存在，只能说明答案指向了某份材料，不能说明那份材料支持这句话。

项目因此区分来源、证据和断言。

| 对象 | 说明什么 | 例子 |
| --- | --- | --- |
| Source | 资料来自哪里 | 一篇论文、一个公开页面 |
| Evidence | 实际取得了哪段内容 | 某论文第 3 页的文本块，或代码的一个窗口 |
| Claim / 回答段落 | 对用户做了什么陈述 | “A 使用了某机制，并在某数据上评估” |
| 核验结果 | 本段是否被引用证据支持、是否满足问题 | 支持、缺证据、漏项、需要补读 |

搜索摘要、关系查询结果和已读原文的角色不同。搜索预览可以指导下一步，但不能因为分配了一个 E 编号，就升级为已经阅读全文的证明。

### 7.1 引用核验做哪些检查？

答案会先拆成段落或检查块，再绑定所引用的原文。程序检查编号和位置等可确定事项，模型检查语义支持和需求覆盖；模型的核验输出也必须符合合同。

论文关系还有单独约束。假如结论是 A→B，必须保留引用方 A 的参考文献依据；A↔B 则需要两个方向。当前核验合同要求论文语境下的段落返回 `citation_relations`，避免只核对一句泛泛的“相关工作”而漏掉具体方向。

如果索引没有识别到边，但已读原文明明存在完整参考文献条目，核验器可以提交证据编号和逐字摘录。程序继续检查它是否属于引用方、是否绑定本段、是否含目标完整题名；跨块引用还要满足同一论文、原文相邻和顺序一致。

即便明确引用成立，也只支持“引用”这个关系。“继承了方法”“复用了代码”“改进了某个算法”仍需要对应的方法或实现证据。

### 7.2 漏项后为什么采用有限修订？

用户要求比较四篇论文，模型可能读了四篇，最后只总结三篇。还可能正确解释了方法，却把一条引用挂到相邻的错误段落上。它们不完全是召回问题。

当前修订尽量保留已经核验的事实段落，把具体漏项、可读位置和证据摘录反馈给模型，再对合并后的答案重新检查。论文必读范围由核验模型依据完整问题返回 `required_document_ids`；单纯在问题里提及一个标题，不等于授权或要求通读该论文。

有具体缺口且还剩预算时，可以经原有工具路径补读。预算耗尽或原文确实不可得时，系统应保留未完成项。把限制说清楚可以构成部分交付，但不能把它改算为严格完成。

### 7.3 核验器本身也会出错

这里有过一个真实工程问题：草稿内容已经正确，但核验响应缺少必填字段或结构不合法，产品因此没有完成交付。

修复没有把异常直接当作“核验通过”。`check_answer()` 对格式错误最多做一次纠正重试，把缺失字段说明反馈给核验模型，并共享原有时间期限；支持状态、段落覆盖等字段仍要显式校验。核验合同升级后，旧断点中的旧核验结果和相关排队状态也需要失效。

这说明核验有双重作用，也有代价：它能够找出无据添加，却额外引入请求、时延和格式失败面。工程目标是让这些失败可定位、可恢复，而不是假设“再接一个 Judge 就可靠了”。

核验实现看 [verify.py][src-verify]，补读和修订如何回到主循环看 [loop.py][src-loop]，两题恢复记录看[第 12 阶段优化历程][history-12]。

<a id="context"></a>

## 八、长任务的上下文和记忆，应该保存到哪里？

研究任务读得越多，发送给模型的上下文越长。全部拼回去会超预算；直接删旧内容，又可能把用户限定、已核验结论或论文身份删掉。

项目把“存下来”和“这次发给模型”分开处理。

| 层次 | 主要作用 | 为什么单独保留 |
| --- | --- | --- |
| 原始档案 | 保存消息、文件、证据与事件 | 压缩摘要丢失细节后仍能回查 |
| 可复用记忆 | 保存有来源、状态和作用域的偏好或发现 | 不把所有旧对话一并塞给新任务 |
| 本轮 context | 放入当前问题、可见历史、证据映射和近期工具对 | 满足模型输入预算与当前任务需要 |

### 8.1 压缩不能破坏工具协议

工具调用和结果通过 call ID 配对。只保留结果，删掉对应调用，下一次请求可能协议错误；只保留调用，删掉结果，又丢了决策依据。

[context.py][src-context] 因此按完整工具往返处理消息，保护问题和必要映射，裁剪旧观察。语义压缩则把较早进展整理成 goal、constraints、completed、pending、findings、conflicts 等结构化字段，并保留证据身份和近期工具对。

摘要里的“已读”尤其需要谨慎。一篇候选可能只打开过首页，不能在压缩后变成“已完成方法与引用核验”。当前论文获取进度也保留已读证据、失败地址和后续位置，让补读不依赖一段模糊总结。

语义压缩失败时，已有有效状态不能被失败结果覆盖；系统可以退回本地有界裁剪。受保护内容仍放不下时应明确失败。这里的 token 预算采用 UTF-8 字节数除以 3 的估算，不是供应商的精确 tokenizer 计数。

### 8.2 跨会话记忆不能变成“什么都自动共享”

同一个研究区共享资料，但新建会话有独立聊天历史；显式分支才继承选定轮次之前的内容。记忆区分 session、workspace 和 global，当前合同只允许偏好成为 global。

一条模型生成的发现还需要来源和证据检查，不能自动当作事实。忘记操作通过 tombstone 阻止同内容再次自动提取，相关变更会让旧执行上下文失效；它不等于物理擦除了所有原始聊天。

读 [memory.py][src-memory] 的可见性规则，再读 [sessions.py][src-sessions] 的历史边界，会比先看“智能记忆”这个名字更容易理解实现。学习练习见[记忆与上下文教程][chapter-context]。

<a id="recovery"></a>

## 九、任务恢复：保存聊天记录为什么还不够？

假设模型已经选定一个工具，工具也返回了结果，这时服务退出。重新发送原问题，会重复执行；只恢复聊天文本，又不知道已消耗预算、待核验草稿和工具队列处于哪里。

项目分别保存三类记录：Trace 描述发生了什么，会话事件给前端展示进度，执行检查点保存下一步继续运行所需的结构化状态。

SQLite 中的 Job 经过 queued、running，再进入 completed、failed、cancelled 或 interrupted。请求接口提交任务后，worker 执行较长的研究流程；FastAPI 的 SSE 事件流让前端持续看到状态，而不是一直等待一个长 HTTP 回答。

### 9.1 Resume 和 Retry 为什么分开？

Resume 从有效检查点继续同一 Job；Retry 新建 Job，并保留与旧任务的关联。恢复前要检查所属研究区、记忆版本、活动任务冲突和检查点是否有效。

恢复时还会增加 `execution_generation`。例如旧请求属于第 0 代，用户恢复后已经到了第 1 代；旧请求迟到返回时，不能只因为 Job ID 一样就覆盖新结果。关键事件和检查点写入会检查运行状态与代次。

这类细节不是展示界面能解决的。取消和完成发生竞争时，后到的完成回调不能把已取消任务重新标成成功。

### 9.2 不能保证所有外部动作 exactly-once

工具实际完成、结果提交检查点之间仍有时间窗口。已提交结果可以复用，有身份的委派可以幂等；但未知结果的付费请求或实验，不能直接假定“没记录就是没执行”。

因此项目实现的是基于已提交检查点的恢复和关键操作幂等。取消也不能保证服务商立即停止一个已经发出的请求。未知用量需要保留为未知，不能填零制造低成本结果。

这些机制可以在 [sessions.py][src-sessions]、[trace.py][src-trace] 和 [usage.py][src-usage] 中找到。更完整的生命周期解释见[恢复教程][chapter-recovery]。

<a id="auto-research"></a>

## 十、从调研到实验：Auto Research 多了一层什么？

回答“论文用了什么方法”，和“基于这个方法做一个可比较实验”，是不同任务。后者需要保存假设、实现代码、运行评测，再根据实际结果决定是否继续。

Auto Research 在普通研究 Agent 外面增加一层控制器。研究模型选择调研、保存方案、委派编码、执行实验、检查结果或结束；内层 ResearchAgent 完成一次定向资料研究。

### 10.1 研究模型、Codex、宿主各做什么？

| 角色 | 负责的工作 | 交付依据 |
| --- | --- | --- |
| 研究主控 | 明确问题、选择动作、形成假设与方案 | 方案版本、引用和决策记录 |
| Codex 编码 Agent | 在约定工作区实现局部方案 | 实际源码变化和产物 |
| 宿主执行器 | 执行实验、记录收据、核对支持的指标合同 | 退出码、结果文件、预测、评分与源码身份 |

Codex 在总结里说“提升了 5%”，不会自动变成实测成绩。宿主需要回收实际产物；baseline、candidate、ablation 要有各自的角色和结果路径。在支持的评测合同内还能复算预测和指标。

不过，可复算只证明评分链条可以检查，并不自动证明 baseline 的实现公平或者研究结论具有泛化性。这两部分仍需要设计审查和更多实验。

实验需要调用模型时，可以通过宿主管理的 JSONL 批次或 stdio 交互接口取得响应。凭据由宿主管理，用量归入任务预算；编码和实验不需要自行持有真实密钥。

### 10.2 为什么只做有限的双路研究？

有些子问题可以独立做，例如一条研究方法，另一条研究评测协议。`research_parallel` 一次原子创建两个子研究，每个都有独立模型上下文、证据和检查点；主控等待两路终态，再根据分项结果继续。

如果第二个问题依赖第一个答案，应顺序研究。当前编码和实验仍串行，不能把双路资料调研描述成任意递归的 Agent 群体。

两路证据还必须带上 `(job_id, evidence_id)`。子研究 A 的 E1 和 B 的 E1 不是同一份材料。一路失败时也要把失败传给主控，不能只拼接成功文本，制造整批成功的印象。

项目确实测到过双路任务时间重叠，但一组历史配对总耗时是 **651.984 秒 → 706.297 秒**，没有证明总体提速。共享资源、调度和模型生成路径都可能抵消并发收益。该负结果及口径保留在[并行研究优化记录][history-08]。

### 10.3 报告交付和研究目标达成要分别判断

后台状态 `completed` 表示生命周期结束；Auto Research 的 `reported` 表示交付了报告，`goal_met` 还需要冻结的数值目标获得有效测量支持。假设没有提升、预算耗尽或外部依赖阻塞，都应该成为可解释结果。

目前代码执行主要面向本机 Windows 原生 Codex 环境，存在平台和隔离边界；不能把它当作任意不可信代码的完整虚拟机。普通资料研究无需启用这条执行路径。

阅读顺序：[auto_research.py][src-auto] → [coding_tool.py][src-coding] → [experiment_process.py][src-process]。具体操作和合同看[Auto Research 教程][chapter-auto]及[并行研究教程][chapter-parallel]。

<a id="evaluation"></a>

## 十一、实验到底验证了什么？

指标最容易写错的地方，是把不同任务、不同层次的数字放在同一句里。检索召回率衡量材料有没有进入候选集；任务完成率衡量最终答案是否满足要求；交付率帮助定位任务有没有走完产品流程。

### 11.1 相比单次 API＋Prompt，哪里有明确收益？

现有对照使用固定 24 题，覆盖 QASPER、SciFact、HotpotQA 和项目公开资料问答。直接 API 基线使用原始材料与提示词，按原语料顺序装入预算能容纳的完整段落前缀，一次作答，不使用项目检索、工具或修订。

双方模型配置为 `deepseek-v4-1-flash`、temperature=0，单次上下文预算 16K；Agent 可以多轮访问整个资料库。表中 Agent 使用首次运行成绩，API 使用冻结流式协议后的整组结果：

| 任务范围 | 单次 API＋Prompt | ResearchAgent | 可以支持的判断 |
| --- | ---: | ---: | --- |
| 全部固定 24 题 | 12/24，50.0% | 18/24，75.0% | 在该系统对照中多完成 6 题 |
| 资料超过单次上下文的 14 题 | 5/14，35.7% | 12/14，85.7% | 大资料库的信息获取与组织体现出收益 |
| 其中 SciFact 问答子集，5,183 篇摘要 | 2/6，33.3% | 6/6，100% | 在该 6 题子集上能定位相关摘要并回答 |
| 完整资料可以放入的 10 题 | 7/10，70.0% | 6/10，60.0% | 额外流程也可能产生交付故障 |

14 题分组按预先冻结的输入长度规则划分，包含这一类中的成功和失败；SciFact 六题是其子集，不能把两行相加当成 20 题。

API 基线先前的非流式轮存在 HTTP 正文截断；随后冻结独立流式协议，以逐字相同输入重跑全部 24 题，两轮分别计分。这里采用完整的流式轮，没有按题拼接两轮最好结果。传输修正经过见[指标依据][metrics]。

这组实验支持的是“让系统从完整资料库寻找证据，相比直接装入固定原文前缀有收益”。它不代表已经战胜了人工挑好全部正确证据的长上下文模型，也不是等调用量、等 token 的比较。Agent 记录来自 10-09，API 基线来自 10-10，传输方式和后端时段差异没有完全控制。

同样，**50 个百分点是观察到的差值，不等于统计显著性结论**。这仍是小样本，需要重复运行和置信区间来判断稳定性。完整条件见[对照报告][comparison]。

### 11.2 混合检索的数字，不是问答正确率

在 SciFact 的 **20 道可评分保留题、5,183 篇摘要**检索子集上，BM25 与混合检索的历史结果为：

| 方法 | Recall@5 | MRR@5 |
| --- | ---: | ---: |
| BM25 | 75.0% | 0.604 |
| 混合检索 | 87.5% | 0.714 |

Recall@5 看相关材料在前五个候选中的召回情况；MRR@5 更关注第一个相关结果排得有多靠前。二者都只评价检索层，无法直接推出回答一定完整、引用一定正确。

这里的 20 道检索题与上一节 6 道端到端问答题口径不同。更不能写成完整 SciFact 官方任务成绩。出处和本地原始报告路径见[指标依据][metrics]。

### 11.3 团队论文十题的 80% 和 100% 是什么意思？

这组自建任务使用预先导入的 **5 篇团队论文和 1 篇其他团队干扰论文**，共 10 题，覆盖作者／机构消歧、方法比较、引用跳转、共同引用、无答案控制和多篇关系归纳。

在同题复测中，严格任务完成率从 **6/10 → 8/10**，成功交付率从 **8/10 → 10/10**。两轮使用相同原文、模型和题目，强制本地问答、禁用联网与历史召回；这是已知开发题集上的复测，没有独立专家盲评，也没有 GraphRAG 对照组。

“成功交付”说明产品流程交出了结果，并不说明所有所需证据都已经找到或被正确利用。“严格完成”还要求满足冻结的事实检查项、答案完整、引用充分且没有实质性无据添加。

因此，另外两题虽然交付成功，仍因归纳漏项或引用缺陷未严格通过。调用外部模型 API 不意味着这些缺陷都无法工程改进：拆分要求、保留原文、检查覆盖和局部修订仍可能改善结果。

这组十题没有做同题纯 API 对照。它适合说明论文关系功能的完成情况，不能用它计算相对 API 的提升幅度。详细记录见[指标依据中的团队论文部分][metrics]。

### 11.4 修复两题以后，为什么还保留首次成绩？

24 题中，两条内容已正确但核验结构异常的失败后来通过专项恢复。累计记录因此是 **原先 18 题＋恢复 2 题＝20/24**，超长资料组累计为 13/14。

累计记录回答的是“这些任务最终有哪些完成了”。它复用了历史通过结果和局部检查点恢复，**不是最终版本从头跑完 24 题的新成绩**。因此本文用首次 18/24 做原对照，把累计 20/24 单独解释，也不据此重新计算整组端到端成本。

普通单次 RAG 的完整对照也保留在[报告][comparison]中：该配置首次 20/24，Agent 首次 18/24、专项恢复后累计 20/24。Agent 有补齐证据的独有通过案例，但这份记录不支持整体优于普通 RAG 的结论。保留这个结果，有助于判断什么时候值得支付多轮执行的成本。

### 11.5 成本也属于结果

上述 24 题对照中，API 基线平均耗时 **9.17 秒**，Agent 为 **151.61 秒**；已知任务 token 分别为 **191,198** 和 **1,828,540**，Agent 另有 3 次请求用量未知。Agent 统计包含检索、阅读、核验及产品后处理，两边执行工作量不同。

所以现有实验不能支撑“更快、更省 token”。它呈现的是用额外执行成本换取部分任务的完成和证据可追溯性。下一步若要优化成本，应冻结同一批任务，同时报告质量、时延、调用数、token 和失败率。

项目还记录了历史检索、重复压缩、并行研究和自动实验等专项结果，它们各自验证不同层面。不要把“320 条评测任务”解释成 320 道全部完成的在线问答，也不要把多次运行中的事实检查数当成独立题目数。

<a id="failures"></a>

## 十二、从真实失败里，应该学到什么？

如果只展示最终通过率，很容易以为这些机制是一次设计就齐全的。实际更适合学习的材料，是故障出现在哪一层，以及修改为什么针对这一层。

| 观察到的问题 | 对应改进 | 仍然保留的边界 |
| --- | --- | --- |
| 检索预览短，模型误以为正文缺信息 | 区分预览与原文，提供窗口、偏移和后续位置 | 看过一个窗口仍不等于读完整篇 |
| 多篇论文都读过，成稿时仍漏项 | 保留论文读取清单、具体需求与修订证据 | 最终归纳仍有未通过任务 |
| 正确草稿因核验 JSON 异常未交付 | 共享期限内的一次格式纠正、字段与覆盖检查 | 持续异常仍会保留失败与草稿 |
| 作者同名、上传年份与发表年份混淆 | 署名消歧、版本线索和原文核对 | 元数据与解析都可能不完整 |
| 单向引用被概括为互引或继承 | 显式关系方向与引用方证据绑定 | 引用不能单独证明方法继承 |
| 任务恢复后旧请求返回 | 执行代次检查，拒绝旧回调写入 | 不能撤回已经发给供应商的请求 |
| PDF 获取遇到反爬或访问限制 | 保存失败位置，允许用户下载后导入 | 不宣称所有论文都能自动取得 |

### 12.1 人工下载论文，是流程中的一个明确步骤

之前的四篇论文核验任务，自动获取没有走完。只改善归纳漏项，无法生成根本没拿到的原文；只解决下载，也不能保证最后的关系结论正确。

当前允许用户手动获取受限论文，再上传或导入资料库，由 Agent 继续解析、阅读和核验。实际记录中，人工补齐后另一次完整任务核验了 **4/4 篇候选**的作者、版本和关系。它是一道任务的候选覆盖情况，不是十题任务集进一步升分，也不能描述成全自动联网成功。

这种协作让任务能够继续，同时保留获取方式和证据来源。项目应把人工参与点呈现出来，便于使用者知道下一步可以怎么做。实现和案例记录见[作者关系说明][relations-doc]。

### 12.2 改进顺序应跟着故障层走

找错材料，就检查召回与查询；材料正确但没送进当前输入，就检查窗口和压缩；已经读到仍漏答，就检查需求覆盖和修订；正确答案没有交付，就检查核验合同、超时和恢复。

这些问题可能同时出现。把所有失败都归因于模型能力、反爬或者“缺 GraphRAG”，很难选出有效改动。Trace、原始证据、草稿与最终状态共同保留，才有条件做这种分层诊断。

已有[优化历程][history]按阶段保留改动、结果和不足；其中作者／引用检索与核验恢复集中在[第 12 阶段][history-12]。本文按理解依赖组织章节，不把后来的案例改写成未经记录的历史开发动机。

<a id="practice"></a>

## 十三、怎样边跑边读这个项目？

建议先跑一个不用模型密钥的离线闭环，观察真实状态，再接在线模型。离线样例验证的是执行路径，不代表开放域研究能力。

### 13.1 第一次启动

准备 Python 3.12 和 Git。以下命令在一个新的克隆目录中运行；前端是原生 Web 页面，不需要 Node.js 构建。

```powershell
git clone https://github.com/prossiblezero/researchagent.git
cd researchagent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$env:PYTHONUTF8 = "1"
$env:OFFLINE_MODE = "1"
$env:RETRIEVAL_MODE = "lexical"
.\.venv\Scripts\python.exe server.py
```

打开 `http://127.0.0.1:8000`，创建研究区和会话。接口文档在 `/docs`，可以对照网页操作理解请求合同。按 Ctrl+C 停止服务。服务默认面向本机，没有多用户登录体系。

想先看最短调用链，可以保留上述离线环境设置，运行：

```powershell
.\.venv\Scripts\python.exe main.py "请查明 learn-claude-code 是什么，以及它的仓库地址。"
```

终端会给出回答、来源和 Trace 路径。打开自己的 Trace，找一条工具调用、一条观察和一个终止原因。完成这一步后，再去读 `ResearchAgent.run()`，会比直接面对整个文件容易很多。

Linux / macOS 命令、在线配置和向量准备见[仓库快速开始][quickstart]。真实联网需要模型和搜索配置；Auto Research 的编码路径还需要本机 Codex 环境。

### 13.2 按“一个问题，一条执行链”读源码

| 这一轮要弄清什么 | 先看哪里 | 能讲清什么就算读懂 |
| --- | --- | --- |
| 用户消息如何变成后台任务 | `server.py` → `workbench.py` → `workbench_store.py` | 消息、Job、Run、Report 为什么分开 |
| 模型决定和执行约束在哪里相遇 | `contracts.py` → `models.py` → `loop.py` → `policy.py` | 合法 JSON 为什么仍可能被拒绝 |
| 候选怎么变成原文依据 | `library.py` → `retrieval.py` → `loop.py` | ref_id、父块、E 编号各代表什么 |
| 作者与引用关系如何核实 | `paper_relations.py` → `verify.py` | 同名、共同引用、方法继承如何区分 |
| 漏项怎样回到补读和修订 | `verify.py` → `context.py` → `loop.py` | 为何不能把核验异常当成功 |
| 恢复后怎样挡住迟到结果 | `sessions.py` → `workbench.py` | generation 与 checkpoint 分别解决什么 |
| 实验成绩由谁认定 | `auto_research.py` → `coding_tool.py` | 自述、执行收据和有效测量的区别 |

源码链接在前文对应章节，也可以从[项目地图][chapter-map]按模块进入。不要一开始就背所有类名；先选一个问题，在入口、状态和产物之间来回追踪。

### 13.3 运行现有离线回归，观察不变量

在项目根目录，沿用上面的离线环境，可以选择相应测试：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_paper_relations.py' -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_verification_flow.py' -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_context_memory.py' -v
```

这些测试主要使用固定输入检查合同与故障处理，不能替代在线评测。读测试时，找出输入变了什么、期望拒绝什么、系统留下了什么状态，而不只是看绿色的通过数量。

完整离线验收沿用 CI 命令：

```powershell
.\.venv\Scripts\python.exe evals/run_all.py
.\.venv\Scripts\python.exe evals/run_v1_eval.py --output evals/reports/v1-ci.json
.\.venv\Scripts\python.exe evals/run_v2_eval.py --output evals/reports/v2-ci.json
```

评测输入、标签和许可证说明在 [datasets/][datasets]，核心脚本在 [evals/][evals]。公开仓库保留源码、任务输入、测试与报告摘要；大量历史原始请求、数据库、Trace 和本地研究档案不随源码上传。报告里的历史数字有来源索引，不意味着克隆后可以直接拿到每份原始运行档案；公开范围见[发布说明][publication]。

<a id="next"></a>

## 十四、还有哪些问题值得继续做？

后续最有价值的工作，需要能回答一个明确问题，并有可复算的对照。

**先确认修复后的整体质量。** 用冻结的完整题集从头重跑最终版本，保留每题首次结果与失败，并增加重复次数。这样才能把累计恢复记录升级为同条件的新测量。

**再判断何时值得多轮执行。** 以相同检索层比较单次回答、按缺口升级的流程和当前 Agent，同时记录完成率、时延和总用量。材料很短的问题可能不需要承担全部流程成本，这需要实现和验证后再下结论。

**用更大的关系任务检验图能力。** 扩大作者同名、版本变化、引用方向、跨团队主题和全局归纳任务，分别观察实体对齐、显式边检索与社区摘要的贡献。只有控制变量，才知道 GraphRAG 补的是哪类缺口。

**把归纳漏项单独拿出来测。** 固定已正确取得的原文，比较需求拆分、证据组织、压缩和局部修订对完整性的影响。这样能够区分材料获取问题与生成利用问题。

**继续验证边界情况。** 复杂 PDF 署名、扫描件、不可下载原文、模型响应异常、取消竞争和未知外部执行，都值得加入回归。系统需要让这些情况可解释、可继续，而不是只在一次理想演示中走通。

学到这里，可以尝试不用看文档，完整讲一遍：用户提出团队论文问题后，材料怎样进来，模型怎样决定下一步，原文怎样支持结论，中断后怎样继续，最后凭什么说任务完成。讲得清这条链，再结合一个真实失败和一组有边界的实验结果，就能把这个项目的工作说明白。

---

继续阅读：[分章源码教程][tutorial] · [优化历程][history] · [实验对照][comparison] · [指标依据][metrics] · [GitHub 仓库][repo]

[repo]: https://github.com/prossiblezero/researchagent
[snapshot]: https://github.com/prossiblezero/researchagent/tree/7e3a3be3b735d0859fbd767285a5dab9f0c88550
[reference]: https://yyhdbl.github.io/
[tutorial]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/README.md
[quickstart]: https://github.com/prossiblezero/researchagent#快速开始
[datasets]: https://github.com/prossiblezero/researchagent/tree/main/datasets
[evals]: https://github.com/prossiblezero/researchagent/tree/main/evals
[publication]: https://github.com/prossiblezero/researchagent/blob/main/docs/github-publication.md
[history]: https://github.com/prossiblezero/researchagent/blob/main/docs/optimization-history/README.md
[history-08]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/docs/optimization-history/08-parallel-delivery.md
[history-12]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/docs/optimization-history/12-agentic-rag-and-verification-recovery.md
[comparison]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/docs/research/rag-comparison-20261011.md
[value-report]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/docs/resume/researchagent-value-20261010.md
[metrics]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/docs/resume/researchagent-20261009-evidence.md
[relations-doc]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/docs/author-citation-retrieval.md
[chapter-map]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/00-project-map.md
[chapter-loop]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/02-agent-loop.md
[chapter-retrieval]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/05-retrieval.md
[chapter-context]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/06-memory-context.md
[chapter-recovery]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/07-recovery.md
[chapter-auto]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/08-auto-research.md
[chapter-parallel]: https://github.com/prossiblezero/researchagent/blob/main/docs/tutorial/09-parallel-research.md
[src-server]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/server.py
[src-workbench]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/workbench.py
[src-store]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/workbench_store.py
[src-loop]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/loop.py
[src-contracts]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/contracts.py
[src-models]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/models.py
[src-policy]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/policy.py
[src-library]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/library.py
[src-retrieval]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/retrieval.py
[src-relations]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/paper_relations.py
[src-verify]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/verify.py
[src-context]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/context.py
[src-memory]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/memory.py
[src-sessions]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/sessions.py
[src-trace]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/trace.py
[src-usage]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/usage.py
[src-auto]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/auto_research.py
[src-coding]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/coding_tool.py
[src-process]: https://github.com/prossiblezero/researchagent/blob/7e3a3be3b735d0859fbd767285a5dab9f0c88550/research_agent/experiment_process.py
