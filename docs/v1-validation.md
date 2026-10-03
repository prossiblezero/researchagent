# V1 验收记录

日期：2026-09-16。目标：`D:\project\researchagent`。Python：evidence-agent / 3.12.14，Windows 11。

## 结论

V1 已完成工程实现和验收：研究区、会话、六类结构化意图、后台任务、取消/重试、重启恢复、报告文件及元数据、旧数据库兼容、页面和 CI 配置。最终真实模型链路成功生成并保存带引用的研究报告。

这些结果说明工作流能运行；真实报告尚未经过人工或版本化 Judge 标注，不代表研究正确率、事实支持率或通用任务成功率。V2–V4 未在本次实现。

## 实际执行

| 检查 | 结果 |
|---|---|
| 目标仓库 `python -m unittest discover -s tests -q` | **102/102**，退出码 0 |
| 同版源码隔离副本 `python evals/run_all.py` | 102 项测试；Stage 1 **4/4**；离线 **21/21**；Stage 3–7 PASS；退出码 0 |
| 最终目标 V1 离线工作流 | **6/6**，退出码 0，见下方不可覆盖报告 |
| 最终目标真实 V1 工作流 | **4/4**，退出码 0，实际 6 次成功外部检索，29 条来源，报告完成 |
| 本地 HTTP 与浏览器 | 创建两个研究区、问候不建研究任务、研究进度/报告、刷新历史、研究区隔离均通过 |
| 页面脚本 | JavaScript 语法检查通过；浏览器实际布局与操作通过 |
| 迁移 | 从旧 runs/evidence 表迁移，旧答案及正文逐值保留；重复初始化通过 |
| CI | 已添加 Windows/Linux Python 3.12 工作流；未推送，远端执行未发生 |
| Docker | Dockerfile 与 `.dockerignore` 已更新；本机无可用 Docker 命令，镜像构建未执行 |

新增 14 项测试覆盖实际状态流转和错误路径，其中两个是在真实运行失败后补充：本地压缩摘要的思考模式协议字段，以及工具预算结束时明确要求总结已有证据。前者在旧代码上复现 `model_error`，修复后通过；后者复现工具列表被撤回但模型仍生成文本工具请求的情况。

工程验收入口：`evals/run_v1_eval.py`。每次使用独立数据库/Trace/报告目录，不写用户默认运行数据库；指定输出路径已存在时拒绝覆盖。报告保存源码 SHA-256，最终交付清单见 `v1-files.sha256`。

## 工件

- [最终离线报告](../evals/reports/v1-offline-accepted-20260916.json)：CHAT 不建任务、研究进入队列、生成报告、研究区隔离、失败可重试、关闭后 interrupted，共 6 项。
- [最终真实报告](../evals/reports/v1-live-budget-summary-20260916.json)：CHAT、RESEARCH 入队、有效引用报告、区间隔离，共 4 项；不是 4 道研究题的成功率。
- [真实研究 Markdown](../data/v1-eval/v1-live-da80c04a4f/reports/63c079b4806f4ccf854a29aa7178d4b7/0d8b6e77c502468ca39a66fedbdf5314/report.md)。问题为“我想研究一下 Agent”。
- [真实研究 Trace](../data/v1-eval/v1-live-da80c04a4f/traces/20260916T080829-f90c90ef.jsonl)。该次报告使用搜索摘要，未进行论文全文解析，也不表示已通读论文。
- 原源码备份：`data/v1-backups/pre-v1-20260916.zip`，不含 `.env`、Git 或用户数据库。

data 下工件属于本地运行产物，被 Git 忽略；JSON 报告中的路径相对仓库根目录。向其他环境交付证据时需要一并携带报告引用的 `data/v1-eval/<run>/`，不能只复制报告 JSON。

## 失败记录与修复

所有以下运行都确实发生，保留失败工件，没有覆盖成成功结果。它们不是同版配对评测，不计算跨版本的性能提升百分比。

| 报告 | 结果与后续 |
|---|---|
| [v1-live-20260916](../evals/reports/v1-live-20260916.json) | Python 文档样例完成两次搜索后，请求未授权的 URL，`read_not_authorized`；保持拦截策略，不扩大授权 |
| [v1-live-agent-20260916](../evals/reports/v1-live-agent-20260916.json) | 早期简报过宽，3 次工具预算后模型继续输出文本工具调用，引用检查失败；简报收窄为 1–3 个子问题，联调改用实际工作台的 6 次预算 |
| [v1-live-final-20260916](../evals/reports/v1-live-final-20260916.json) | 压缩产生的本地 assistant 摘要缺少 `reasoning_content`，DeepSeek 返回 HTTP 400；补充空字段，保留真实轮次的推理字段，未伪造推理 |
| [v1-live-final2-20260916](../evals/reports/v1-live-final2-20260916.json) | HTTP 400 已消失，但预算用满后模型仍请求工具，报告被引用校验阻止；补充受保护的 FINAL_ONLY 总结指令 |
| [v1-live-budget-summary-20260916](../evals/reports/v1-live-budget-summary-20260916.json) | 最终 6 次工具预算内完成，任务 completed，Markdown/HTML 与数据库元数据均保存 |

安全和事实边界未放宽：未知 URL 仍不能读、无合法引用仍不能假装完成、失败显示原因并允许重试。默认 CLI 和原 H1–H3 门禁继续通过。本次修复仅在 researchagent 落地，没有修改 newagent 或启动 H4。

## 真实 Trace 怎么看

最终成功运行中的原始 JSONL 行（已由运行时脱敏）：

```json
{"citations": ["S2"], "event": "run_finished", "network_requests": 6, "run_id": "20260916T080829-f90c90ef", "seq": 52, "source_count": 29, "status": "ok", "termination": "model_final", "tool_attempts": 6, "tool_calls": 6, "tool_denials": 0, "tool_successes": 6, "ts": "2026-09-16T08:09:21.799+00:00"}
```

`run_id` 与数据库 runs、job.run_id 和报告中的 Run 字段相互关联；`seq=52` 是该运行第 52 条事件，`ts` 是 UTC 时间。`tool_calls`/`tool_successes` 为逻辑调用计数，`network_requests` 为外部网络请求计数，本次均为 6；`source_count=29` 是来源条数，不是正确事实数。`citations` 仅列 S 类引用，这份答案还使用 E 类 Evidence 引用，不能把数组长度当成全部引用数量。

`status=ok` 和 `termination=model_final` 表示 Harness 完成了合法输出检查，工作台随后成功保存文件和元数据才变为 completed；它们不是语义正确性的人工判断。`quality_judgment=UNJUDGED` 因而保留。

## 已知边界与恢复

- 单用户、单机、一个数据库对应一个服务进程；后台最多 16 个活跃/排队任务。
- 页面断开不影响任务。服务重启后不会自动续跑或重放外部调用，未完成任务变为 interrupted，用户显式重试。
- 取消等待当前有超时上限的网络请求返回，随后停止；不强行杀线程。
- 仍使用既有 lexical Claim verifier。真实报告中弱证据、二手摘要、争议或不足应由用户核查；H4 语义验证不属于本次交付。
- 原 H1–H3 的同步哈希是历史快照；V1 回调与上述两个真实缺陷修复改变了 loop.py、trace.py、context.py，新的哈希另行记录。
- 代码与运行数据分别备份；恢复原源码时保留数据库及报告，不删除旧数据重建。
