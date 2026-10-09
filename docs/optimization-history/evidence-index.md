# 证据与原始报告索引

本表于2026-10-04从实际文件计算 SHA-256。阶段文章保留公开可读的结果表；这里提供原报告的精确定位及版本指纹。`evals/reports/` 下的文件属于本地研究档案，未随精简公开仓库上传，故使用代码路径而非失效链接。公开文档/协议则提供正常链接。

历史文件中的“当前”“尚未完成”均以其写作日期为准；后续结果在阶段文章中明确更新。全文哈希锁定本次整理所依据的文件内容，不证明其中每项历史判断永久成立。

| 阶段 | 来源与范围 |
| --- | --- |
| [01](01-foundation-memory.md) | [docs/h1-h3-sync.md](../../docs/h1-h3-sync.md)<br>H1–H3 的同步范围与固定回归结果。 |
| [01](01-foundation-memory.md) | [docs/v1-validation.md](../../docs/v1-validation.md)<br>V1 102 项回归、6/6 离线与4/4真实工作流检查；早期失败保留。 |
| [01](01-foundation-memory.md) | [docs/v2-validation.md](../../docs/v2-validation.md)<br>V2 真实任务由6/15、9/15迭代至14/15；不同修订非独立重复。 |
| [01](01-foundation-memory.md) | `evals/reports/context-memory-optimization-20260918/report.md`（本地）<br>压缩22/24、协议24/24；记忆20/20；关键词/混合检索对照。 |
| [01](01-foundation-memory.md) | `evals/reports/session-mainstream-20260921/report.md`（本地）<br>多独立会话、共享资料与历史分支的修订验收。 |
| [02](02-v3-evidence.md) | `evals/reports/roadmap-v3-20260921/report.md`（本地）<br>报告/来源/关系/假设/实验版本；真实ReAct与Retroformer案例。 |
| [03](03-feedback-strategies.md) | `evals/reports/harness-strategies-20260921/report.md`（本地）<br>查询规划R5 64.92→72.14；问答5/8→6/8；核验超时与不完整计量。 |
| [04](04-v4-controlled-experiments.md) | [docs/v4-ranking-iteration-log-20260925.md](../../docs/v4-ranking-iteration-log-20260925.md)<br>首轮R5退化、原因、开发选优、原文重排与失败回放。 |
| [04](04-v4-controlled-experiments.md) | [docs/iteration-20260927-28.md](../../docs/iteration-20260927-28.md)<br>有限三轮、两组真实问答、阅读/引用修复及保留原始分数。 |
| [05](05-benchmark-real-product.md) | `evals/reports/product-benchmark-20260929/report.md`（本地）<br>真实生产检索及32题两臂完整问答，任务/可评分分母分别列出。 |
| [05](05-benchmark-real-product.md) | `evals/reports/product-benchmark-repair-20260929/scifact-qa-r1/repair-summary.md`（本地）<br>固定8题×2配置修复前后比较，5/16→12/16。 |
| [05](05-benchmark-real-product.md) | `evals/reports/product-benchmark-repair-20260929/scifact-qa-r1/metrics.json`（本地）<br>修复后各配置6/8及逐项指标；非全32题重跑。 |
| [06](06-auto-research.md) | [docs/auto-research-v4-redesign-20260929.md](../../docs/auto-research-v4-redesign-20260929.md)<br>从固定实验循环到可选Auto Research的设计；文内未来计划不计成绩。 |
| [06](06-auto-research.md) | `evals/reports/v4-delivery-audit-20261002/report.md`（本地）<br>真实SciFact闭环的后验复核，区分流程交付和方法有效性。 |
| [06](06-auto-research.md) | `evals/reports/v4-delivery-audit-20261002/r2-method-findings.json`（本地）<br>iteration-r2的47/809训练claim句索引错误与方法限制。 |
| [07](07-amem.md) | `evals/reports/amem-corrected-measurement-audit-20261002/report.md`（本地）<br>完整开发40题、同788轮记忆，修正MMR的三组比较。 |
| [07](07-amem.md) | [datasets/project/auto-research-amem-holdout-v1/PROTOCOL.md](../../datasets/project/auto-research-amem-holdout-v1/PROTOCOL.md)<br>独立留出预设条件；协议文件随公开数据集提供。 |
| [07](07-amem.md) | `evals/reports/amem-holdout-final-audit-20261003/report.md`（本地）<br>留出三组40题完成，但MMR未达到预设条件；失败与费用。 |
| [07](07-amem.md) | `evals/reports/amem-holdout-final-audit-20261003/comparison.json`（本地）<br>三组原始预测独立复算、逐题差异及指标。 |
| [07](07-amem.md) | `evals/reports/amem-holdout-final-audit-20261003/costs-and-archive.json`（本地）<br>最后续接与整轮费用、未知用量、归档收据。 |
| [08](08-parallel-delivery.md) | `evals/reports/v4-delivery-final-20261002/report.md`（本地）<br>普通能力首轮2/3及修复，10-02交付边界；其中A-MEM未完成状态已被10-03终验更新。 |
| [08](08-parallel-delivery.md) | `evals/reports/parallel-research-20261003/report.md`（本地）<br>并行实现、591项回归、33项前端及合入/数据保全记录。 |
| [08](08-parallel-delivery.md) | `evals/reports/parallel-research-final-audit-20261003/report.md`（本地）<br>真实16子任务、合同6/8、总体慢8.33%、原文审阅与失败。 |
| [08](08-parallel-delivery.md) | `evals/reports/parallel-research-final-audit-20261003/summary.json`（本地）<br>每组串并行耗时、任务合同及成本汇总。 |

## 文件指纹

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `docs/h1-h3-sync.md` | 3745 | `bb3b8774b5a9282e01c6681920df722fae51e775c732e7c193814dc753c47445` |
| `docs/v1-validation.md` | 6877 | `a1b645987ccfd68233747900d3e519043d04000587b946253071f9635db14a70` |
| `docs/v2-validation.md` | 7512 | `ff5b8e2bc9639ad9a92d21ea1f19fd2da0d5cba8c66dfb8abee245d6e603ba8a` |
| `evals/reports/context-memory-optimization-20260918/report.md` | 7742 | `bc8f0e825cde8185ee165eaf848882579a7547284bcf62a97c62ef253ea27087` |
| `evals/reports/session-mainstream-20260921/report.md` | 4712 | `ef797686910fab377380f8001b1247cc5dbadb8cddb6b4e459edb5109ead22ce` |
| `evals/reports/roadmap-v3-20260921/report.md` | 10650 | `35559134158630eb490ded62bc7ff62a2d7973fe436b7a7e9fa201434dabc890` |
| `evals/reports/harness-strategies-20260921/report.md` | 15191 | `6bb6c25864af4d22c547d67560c0d101eed6a93191cc12c14b3ede49bcbbccc2` |
| `docs/v4-ranking-iteration-log-20260925.md` | 8902 | `f3dee0702998725eddf7a6e1fbec9abdcb2e282261750712eb10934c498c4ccd` |
| `docs/iteration-20260927-28.md` | 10339 | `831610033096248cffee6d458995e96dd62412e834cd179d82e4205669fe47f0` |
| `evals/reports/product-benchmark-20260929/report.md` | 37489 | `4ba4edfe8a9e60e7932d2f6d1c2155f587bf241a609c6e662bd45289ae0e14c0` |
| `evals/reports/product-benchmark-repair-20260929/scifact-qa-r1/repair-summary.md` | 3329 | `5c108e92815f88829190c4558881a83de3d355cf4cb4c8ae15cac66cf21d2a28` |
| `evals/reports/product-benchmark-repair-20260929/scifact-qa-r1/metrics.json` | 4040 | `0a7cfe4538d380965287a00e41330feabccdb86ea1d38ce84ad6eaef5bc92d9a` |
| `docs/auto-research-v4-redesign-20260929.md` | 25780 | `06b06b005c732e33ff765acfa1323691726615a45e36084e9973870b7ad8af20` |
| `evals/reports/v4-delivery-audit-20261002/report.md` | 9202 | `29ddc6ad798dd7ff278121ace6c61fbebaa50297bbdc11d672237304860db84e` |
| `evals/reports/v4-delivery-audit-20261002/r2-method-findings.json` | 14234 | `b079102c270a0e503638c377aa51dc089d6e20d47020f8e071abfb35bbc37658` |
| `evals/reports/amem-corrected-measurement-audit-20261002/report.md` | 2312 | `8616549e61489a7ed0e3f4fc2249ae3b2bd9734aeea72f3751db8395927eac44` |
| `datasets/project/auto-research-amem-holdout-v1/PROTOCOL.md` | 4556 | `c8640e63b4899c1d6f11c580cd599e9b975a20e5cc2d731cfc4ebd420be5f4ce` |
| `evals/reports/amem-holdout-final-audit-20261003/report.md` | 3613 | `15eefef15a4bbb7f06f28212095f7d29fe60142e1cc8c5e9d8c079d730757b1c` |
| `evals/reports/amem-holdout-final-audit-20261003/comparison.json` | 26756 | `2888b9efc5077b546ecfa55c93479438b3e5dfabed254335640103df69e52f14` |
| `evals/reports/amem-holdout-final-audit-20261003/costs-and-archive.json` | 873 | `c593eb3a80b467965b47be91bf9c32b495a80304ce35a27b4eeedcfa4270782c` |
| `evals/reports/v4-delivery-final-20261002/report.md` | 4156 | `74e5c09cce42053cc119b854af254974574112d24d636b85197c04272e8c2d58` |
| `evals/reports/parallel-research-20261003/report.md` | 2760 | `5d4c868a6cd35caacbf51c53af84ee6e688405aaadb10db30e54be84a4b758fb` |
| `evals/reports/parallel-research-final-audit-20261003/report.md` | 3489 | `be1cc80f100336fe9996913adbf05e712b9d0aed064c3fc9bd3ede6818008b26` |
| `evals/reports/parallel-research-final-audit-20261003/summary.json` | 4494 | `aa1843fb8c11c29271fbb6fcd0f8231b53cfb7217dbb76a4c4e77a74e86e4099` |

## 查阅与复核

在持有本地档案的环境，从项目根目录按表中路径打开报告。可用 PowerShell `Get-FileHash -Algorithm SHA256 -LiteralPath <报告路径>` 对照指纹；报告内进一步指向题面、来源、回答、运行轨迹、成本和失败。不同阶段的原始产物范围不同，不能仅凭索引推断每项都具有完整 token 或完整全文阅读。

在公开仓库中，可直接阅读本目录的阶段结果，查看 [datasets](../../datasets/README.md)、[tests](../../tests) 和 [evals](../../evals) 中保留的输入及核心验证代码。部分早期一次性脚本未公开，复现范围以[发布说明](../github-publication.md)为准。


## 2026-10-09：FastAPI 迁移验收指纹

[迁移过程](11-fastapi-service.md)记录改动、修复及回归结果。以下路径是本地验收档案，未将日志、SQLite 或运行目录上传；哈希按实际保存文件计算，早期失败保留。

| 本地文件 | 字节 | SHA-256 |
| --- | ---: | --- |
| `data/fastapi-migration-20261009/final-offline.log` | 3370 | `d1015a8067415c1571791c9a2995e8a319e1aa038142fb247e12adf33a474172` |
| `data/fastapi-migration-20261009/final-frontend.log` | 3251 | `3eae3d5eb4d4e7c439b114f45f87220646eadc46ed32ce4ced937b2b780216d7` |
| `data/fastapi-migration-20261009/final-v1.json` | 18451 | `cbfd2e4a31d83ac6415926ec80380ae02500d8f97712bed66a0689e18d231673` |
| `data/fastapi-migration-20261009/final-v2.json` | 5561 | `d96f1f0c8dcee9245b7a475c07ba37b20830472f5d8fde852ee797f1a4a34fb0` |
| `data/fastapi-migration-20261009/entrypoint/result.json` | 671 | `2fc0fcafbaac3f4bb7726941fca356a2db0986c20dda677a24d9ddf90137dc1c` |
| `data/fastapi-migration-20261009/final-source-sha256.json` | 42150 | `c8545961ded9e7aca7239f05aec0001adb464433c8dc8bd59c112c595aa3f142` |
| `data/fastapi-migration-20261009/http-regression-r1.log` | 11563 | `8a0408a4b1fc678676d069ebbebc4f960bb60624934614b376d5c22efe743295` |
| `data/fastapi-migration-20261009/http-regression-r2.log` | 1294 | `ecad7bd2b5977f1f37b12e62bb3952af79a4f5c57f58e6bc3c9e2c648550dd8f` |
| `data/fastapi-migration-20261009/http-regression-r3.log` | 2881 | `6d76a4dc93532c9c5ed0947866caf1348f5c906271e1bdb8d709e55677848cb5` |
| `data/fastapi-migration-20261009/http-regression-r4.log` | 362 | `627d712247d0f05a7ff0611e93ec0ac47cd381ad5853fcf6734bacf9bd7d674c` |
