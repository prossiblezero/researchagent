# Auto Research 快速决策评测

`evals/run_auto_research_decisions.py` 用真实模型调用生产 `AutoResearch.decide()`，只评测下一步动作，不执行模型提出的工具。它适合低成本检查控制器修改，不能代替完整研究或独立留出实验。

## 冻结与复跑

面板位于 `datasets/project/auto-research-decisions-v1.json`，包含来自 3 次历史研究的 8 个开发决策点。需要本地 `evals/reports/` 中对应的原始运行；源码导出不附带这些历史档案。首次使用先冻结：

```powershell
.\.venv-v3\Scripts\python.exe evals/run_auto_research_decisions.py freeze --out evals/reports/decision-cases
.\.venv-v3\Scripts\python.exe evals/run_auto_research_decisions.py run --cases evals/reports/decision-cases --out evals/reports/decision-before --model default
```

修改控制器后，用**同一个已冻结目录**运行到新的 `--out`，保持模型配置不变。输出目录必须不存在，脚本不会覆盖旧结果。`run` 会将控制器必要上下文发送到所选模型的配置服务；`freeze` 与测试均不调用模型。脚本最多记录 16 次服务尝试、总运行预算 600 秒；25 万已报告 token 是后续请求的停止阈值，不能阻止正在执行的请求超出该阈值。

冻结过程按历史请求时间重建对话前缀，并拒绝未来才完成的任务或方案。模型看不到该决策的原答案、评分规则或后续结果。快照及清单有 SHA-256；运行前检查输入是否改变。外部工具与状态持久化使用隔离的回放适配器，提示构建、历史裁剪、比较生成仍调用生产代码。

## 如何读结果

- `condition.json`：模型、端点哈希、预算、输入清单及源码哈希。
- `requests/`：每次实际服务尝试的输入、输出、错误、时延及已报告 token；密钥脱敏。
- 各案例的 `result.json`：模型动作与可检查的规则结果。
- `metrics.json`：完成数量、规则通过率、请求失败、用量覆盖率及全部案例；失败保留在分母中。

当前规则检查参数/结果路径一致性、累计预算、有效测量后的 assessment、失败后的补查或重测，以及没有当前目标依据时是否误报 `goal_met`。这是有意收窄的规则：合理的其他工具顺序也可能未通过，必须查看实际动作。`inspect` 通过只代表可接受的中间动作，不表示问题已经修复。`unsupported_goal_met_count` 只检查结构化 outcome，不检验自然语言摘要的所有声称。

8 个决策点不等于 8 次独立研究；多个点来自同一运行。不得只根据这组分数、耗时或 token 差异声称科研效果显著提升。确认改进应使用后续未见研究任务、相同预算与自主性相当的基线，并独立审计真实预测、目标达成和全部失败成本。`scientific_effect_verified` 始终保持 false。

## 长研究中的执行反馈

控制器的 `recent_execution_feedback` 从最近 3 个编码/执行任务的宿主记录生成，保留有效测量数、错误及首个无效测量的命令和日志尾部。整体有长度上限；过长命令会明确省略，完整内容通过 `inspect(kind=coding,id=task_id)` 恢复。早期工具交互被裁剪后仍可看到这份摘要。

这份摘要不复制编码器的自测分数，也不推断历史失败是否尚未解决。判断方法效果仍以当前可比测量和 assessment 为准。需要廉价检查时，可使用独立且不含 `model_requests` 的 diagnostic 调用；同次批量模型请求先于实验命令执行，不能靠把 diagnostic 排在命令首位来节省该批调用。

相关回归位于 `tests/test_auto_research.py` 和 `tests/test_auto_research_decisions.py`。仓库完整离线检查为 `python evals/run_all.py`。

真实短研究闭环及直接自主 Codex 对照见 [同预算短任务评测](auto-research-short-evaluation.md)。该评测单独统计主控与编码的合计用量，通过冻结提交和隐藏样本检验方法效果；不能用上面的单步动作通过率替代。
