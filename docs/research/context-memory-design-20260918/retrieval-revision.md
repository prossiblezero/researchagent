# 检索选型修订：BGE-M3 + ChromaDB

2026-09-18。用户确认“BPE”是指 BGE，并提出 ChromaDB 或 PostgreSQL + pgvector。本文更新上一版设计取舍，未安装依赖、下载模型或修改产品代码。

## 决定

当前采用 BGE-M3 + Chroma 本地持久化 HNSW，保留 SQLite 保存会话、资料、记忆、引用与 FTS5/BM25。查询由 LLM 通过 retrieve 触发；词法与向量召回、过滤和融合由确定性检索管道完成。

原 E5-small + NumPy 方案是小语料最小实现：全文搜索用倒排索引，语义部分逐个比较向量，复杂度随 N×d 增长。它并非没有检索能力，但不应作为资料库增长后的长期主方案。保留为速度/召回对照，正式路径使用已有的 HNSW 实现。

## 方案比较

| 方案 | 快速检索来源 | 合适之处 | 代价 |
|---|---|---|---|
| SQLite FTS5 + NumPy 精确余弦 | 词法倒排；向量全量比较 | 小语料、精确召回基线 | 向量部分线性增长；自行维护持久化/更新代码 |
| SQLite + Chroma | FTS5/BM25 + Chroma HNSW | 当前单机工作台，迁移范围小，可在进程内持久化 | 两个存储的同步、删除和版本需管理 |
| PostgreSQL + pgvector | SQL/全文索引 + HNSW 或 IVFFlat | 需要统一关系数据、向量、事务和关联/权限查询时 | 引入 PostgreSQL 运维，并迁移现有 SQLite SQL、FTS、事务和备份流程 |

pgvector 默认仍是精确检索；必须建立对应 ANN 索引才能取得预期的 ANN 性能。过滤查询还需测召回，必要时使用 iterative scan、合适的元数据索引或分区。迁移 PostgreSQL 并不自动解决中文分词或语义召回。

## 模型与检索合同

- 模型：BAAI/bge-m3，固定 revision `5617a9f61b028005a4858fdac845db406aefb181`；1,024 维，多语言，最大输入 8,192 token。
- 使用 dense 向量，当前不接入模型的 sparse/ColBERT 分支。通过官方支持的 Sentence Transformers 加载；不沿用 E5 的 query:/passage: 前缀。
- 初始分段 512 tokenizer token、重叠 64，保留页码、父段、内容哈希；长输入能力不等于应把整篇论文放进一个向量。
- Chroma PersistentClient，cosine HNSW；应用显式传入 BGE 生成的文档/查询向量。按模型版本隔离集合，检索时服务端限定研究区。
- Chroma top 40 + SQLite FTS5/BM25 top 40 → RRF → 去重与来源多样性 → top 8 → 需要时回读原文。
- Chroma 的文档包含/正则过滤不同于 BM25 排名；不能把云端 Search API 的混合排名能力直接假定为本地客户端能力。
- reranker 在这一流程质量实测后决定是否加入，不默认叠加额外推理成本。

SQLite 为权威数据源，Chroma 为可重建索引。确定性索引 ID、pending/indexed/failed 状态、幂等 upsert、命中后回表校验与删除优先失效用于处理跨库没有共同事务的问题。恢复、分支与记忆策略不因更换向量索引而改变。

## 验证

BGE-M3 的 embedding 耗时、索引构建、向量检索、词法查询、融合分别计时，并报告端到端 P50/P95。用真实标注资料测 Recall@k，同时用精确余弦结果测 HNSW 近似索引召回；后者不等同于语义检索质量。

当前只核对官方资料和版本元数据，未跑 BGE/Chroma 性能测试。硬件不足时保留全文检索并明确状态，不承诺 CPU 一定达到原方案 2 秒目标。完整阶段与验收仍以更新后的 [实施方案](implementation-plan.md) 为准。

## 已读取的一手来源

- [Chroma 本地持久化客户端](https://docs.trychroma.com/docs/run-chroma/clients)
- [Chroma HNSW 配置](https://docs.trychroma.com/docs/collections/configure)
- [Chroma 元数据过滤](https://docs.trychroma.com/docs/querying-collections/metadata-filtering)
- [Chroma 文档内容过滤](https://docs.trychroma.com/docs/querying-collections/full-text-search)
- [BGE-M3 官方模型说明](https://huggingface.co/BAAI/bge-m3)
- [固定版本模型加载模块](https://huggingface.co/BAAI/bge-m3/blob/5617a9f61b028005a4858fdac845db406aefb181/modules.json)
- [pgvector 官方说明](https://github.com/pgvector/pgvector#hnsw)
