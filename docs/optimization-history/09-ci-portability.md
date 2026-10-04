# 09 · 修复 GitHub Actions 的跨平台评测路径

日期：2026-10-04。修复基线为公开仓库 `5ded43ac74d165fbbc63a8359e3cb14f2530bd56`。

## 实际失败

上传优化历程后，GitHub 的 [Offline acceptance 运行 37211754142](https://github.com/prossiblezero/researchagent/actions/runs/37211754142) 失败。Ubuntu 完成 592 项测试，2 项报错、20 项按平台或条件跳过；Windows 被矩阵的 fail-fast 连带取消。源码上传成功，失败发生在自动测试阶段。

`run_evidence_qa_acceptance.seed` 和 `run_mixed_benchmark.seed` 将评测研究区的下载根目录写死为 Windows 的 `D:\paper`。该路径在 Linux 上不是本机绝对路径，因此被 `WorkbenchStore.save_space` 的路径校验拒绝。没有放宽这项校验。

## 修复与回归防护

- 两个失败入口不再覆盖下载根目录，复用已有的默认值：隔离评测数据库旁的 `downloads/<space_id>`。
- 检查同类冻结语料初始化，修正 `run_harness_strategies.corpus` 和 `run_parallel_research_acceptance.run_one` 的相同问题。
- 在既有两个集成测试中增加断言：研究区路径必须位于该次临时评测目录内。修复前，这两个断言在 Windows 上也分别复现失败，不再依赖 Linux 才能发现错误。

改动只影响这些新建评测研究区，保留现有研究区配置和本机实际下载设置。没有修改模型、检索算法、数据标签、指标口径或原始研究结果，没有用跳过失败测试的方式通过 CI。

## 本地验证

在公开发布副本中使用 Python 3.12、`OFFLINE_MODE=1` 执行与 CI 相同的三个命令：

| 检查 | 结果 |
| --- | --- |
| `python evals/run_all.py` | 退出码 0；592 项中 585 通过、7 项默认原生测试跳过；全部离线阶段通过。 |
| `python evals/run_v1_eval.py --output <独立输出>` | 6/6 通过。 |
| `python evals/run_v2_eval.py --output <独立输出>` | 5/5 通过。 |

完整离线检查耗时 129.406 秒；发布副本的本地日志和汇总位于 `data/ci-portability-20261004-r1/`，未随源码上传。测试使用隔离数据库与固定输入，没有运行付费模型或重做历史科研实验。

Ubuntu 和 Windows 的远程复验以[对应修复提交的 Actions 记录](https://github.com/prossiblezero/researchagent/actions/workflows/ci.yml)为准；本地通过不能替代远程结果。此前失败运行继续保留。
