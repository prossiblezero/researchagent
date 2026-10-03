# ResearchAgent 演示与证据入口

本机工作台：<http://127.0.0.1:8000>。普通问答、调研和 Auto Research 共用已有木屋界面；只在用户明确要求实现与实验时启动执行。研究区共享资料、Notes 和设置，独立会话不默认继承历史。

## 现在可打开的真实案例

[只读木屋演示](http://127.0.0.1:8012/)已启动，使用历史SciFact R4的数据库副本和当前产品代码，关闭模型/工作线程并拒绝写请求。点击左侧“已结束”的父任务，查看“研究结果 / 来源与证据 / 实时过程”：方案v1、基线/候选/消融、原文记录、Codex超额后模型主动转宿主测量的轨迹均可见。日常工作台仍在8000。

先阅读[交付复核与更正](../evals/reports/v4-delivery-audit-20261002/report.md)：三组分数独立复算一致；dev的340条候选记录只有339个唯一对；旧子调研整体未核验通过，本轮仅静态接受最终使用的有限原文事实。另一个iteration-r2确有结果驱动改方案并重跑，但证据句索引错位影响47条训练claim，作为失败案例展示。不要将两个案例拼接成一次无人干预成功。

服务未运行时，在项目目录执行 `.venv-v3\Scripts\python.exe -B -X utf8 evals/reports/v4-delivery-audit-20261002/serve_demo.py`。已存在的备份只在核对终态任务身份后复用，不覆盖原案例数据库。当前[14项HTTP检查](../evals/reports/v4-delivery-audit-20261002/product-check.json)和浏览器演示均通过。

## 五分钟演示

1. 在研究区新建两个会话。一个提问“解释一下 Agent memory 的检索与更新”，另一个提问“调研这个方向的论文，暂不实现”。展示普通问答/调研的答案、来源和原文，不启动代码实验。
2. 展示已保存的自动研究案例：目标 → 原文依据 → 待验证方案 → Codex 委派 → 宿主测量 → 研究模型解释和方案版本。任务中的“实时过程”与“来源”可回查；方案标为待验证假设。
3. 展示基线、候选和消融的原始结果、配置、预测与独立评分。解释为什么一次实验运行成功不等于方法有效，为什么允许据负结果结束。
4. 展示中断记录与检查点：保存已完成工作、旧失败和未知费用，再显式续接。不要把人工恢复演示成无人值守恢复成功。

如需新开付费研究，在聊天中明确目标、允许实现/实验以及资源范围。现有验收正在运行时直接查看已有记录即可，无须为演示重复运行。

## 真实案例与可核查产物

| 展示内容 | 入口 | 能说明什么 |
| --- | --- | --- |
| 有限课题完整历史交付 | [SciFact R4复核](../evals/reports/v4-delivery-audit-20261002/report.md) | 同一父任务原文→方案→Codex→三组测量→模型解释；本轮开发代理补做内容/源码审阅，原子报告失败不改 |
| 反馈驱动修订的真实失败 | [iteration-r2方法发现](../evals/reports/v4-delivery-audit-20261002/r2-method-findings.json) | 两版方案和两轮宿主执行，原分数真实但预定证据门控实现有误，不能称方法已验证 |
| 原文阅读、上下文续接和纠错 | [同会话阅读](../evals/reports/source-reading-continuation-20261002/report.md) | 真实继承上一报告并继续读源码；原稿两处过强表述另有更正，不称理解完全正确 |
| 实验结果推动方法版本和编码 | [A-MEM r5 原报告](../evals/reports/amem-live-20261002-luna-r5-fixed-memory/report.md)、[方法审计](../evals/reports/amem-fixed-memory-method-audit-20261002/finding.json) | 实际有两版方案、编码、结果回传；MMR实现错误由人工发现，原报告与失败保留 |
| 修正方法后的三组比较 | [r7 独立审计](../evals/reports/amem-corrected-measurement-audit-20261002/report.md) | 同788轮记忆、三组各40题；F1、R5、R10和EM有取舍，不能把全部增益归于方法 |
| 独立留出测量完成、候选未获验证 | [终验完整报告](../evals/reports/amem-holdout-final-audit-20261003/report.md) | 独立40题/1292轮，三组完整比较与独立复算，MMR未达到冻结收益条件 |
| 检索/记忆等简历指标 | [当前简历](resume/researchagent-20261003-r2.md)、[证据附件](resume/researchagent-20261003-evidence.md) | 产品能力的已有量化结果；不把Codex编码表现或上述局部工程计数替代Harness效果 |

每个实验运行目录保存 `models/` 请求收据、`traces/` 工具轨迹、`coding-tools/` 宿主执行产物与代码快照、`coding-tasks.json` 实验配置、`result-assessment.json` 决策记录，以及原始报告。独立评分在 `independent-audit/`；归档更正另存，不覆盖原模型报告。活动任务的终态文件可能尚未生成。

此前受阻的续接目录为 `evals/reports/amem-live-20261002-luna-holdout-continuation-r1/`，父任务 `6cfdef9b8563491da2a7dbd923cae169`。它使用独立验收SQLite，不自动混入用户主数据库；终态为completed/blocked，三次上游过载；[终态审计](../evals/reports/amem-holdout-terminal-20261002/report.md)为历史受阻记录；最新[2026-10-03终验](../evals/reports/amem-holdout-final-audit-20261003/report.md)已保留旧20题并完成三组各40题。

## 当前完成边界

功能已有完整历史案例及本轮产品演示。SciFact R4的有限任务在披露静态审阅、来源核验和预测存档边界后可复用交付；iteration-r2以真实失败展示反馈修订。不能据此宣称所有课题或当前版本均可无人值守；A-MEM同轮最终续接已完成三组测量和归档，候选未获收益验证；双路并行已完成实现和真实对照，有界交付收尾。完整要求与下一步统一维护在[目标差距](auto-research-goal-gap-20260929.md)，不再为Codex接入单独制造量化指标，也不要求候选必须胜出才交付。

[最终交付清单](../evals/reports/v4-delivery-final-20261002/report.md)与[普通入口修复验收](../evals/reports/ordinary-citation-repair-20261002/report.md)可查。首轮2/3，恢复后真实交付三个HYPOTHESIS；不把恢复成功改写成首轮全通过。

## 双路协作演示

[双路真实报告](../evals/reports/parallel-research-final-audit-20261003/report.md)展示主控、两路独立研究、原文与分项结果；完整归档可离线查看，无需重跑付费任务。它证明双路协作可运行，未证明总体加速；严格合同失败2/8和全部成本保留。
