# Auto Research 与直接 Codex 的短任务对照

这套评测检验一件可负担的事：在相同资源上限内，系统能否通过开发集实验，交付在隐藏样本上优于固定基线的方法。它不检验论文新颖性、完整科研成功率，也不代表公开基准的官方成绩。

## 冻结的第一轮

协议与样本 ID 在 `datasets/project/auto-research-short-v1.json`。任务来自本机已有公开原始缓存，按稳定哈希选择，不根据方法分数筛选。

| 任务 | 开发 / 留出问题 | 文档单位 | 指标 |
| --- | ---: | --- | --- |
| QASPER | 24 / 48 | 论文段落 | 宏平均 evidence recall@5 |
| HotpotQA | 32 / 64 | distractor 文档 | 宏平均 evidence recall@2 |
| LoCoMo | 48 / 48 | 完整对话中的原始话轮 | 宏平均 evidence recall@5 |

QASPER 排除项目此前使用的论文，并限制每篇一问；开发与留出论文不重叠。HotpotQA 排除此前问题，开发与留出上下文标题不重叠。LoCoMo 使用此前四个会话之外的 conv-43/44 开发、conv-47/48 留出；排除 category 5 和无法完整映射证据的问题。LoCoMo 每个 split 只有两个会话，不能把 48 个问题当作 48 次独立研究实验。

三组为优化前 Auto Research（25cdbc7）、当前 Auto Research（3c3e2f8）、直接自主 Codex CLI。前后版本只替换真实 AutoResearch 模块，使用当前相同的 Workbench/CodingTool；两提交的产品差异即该模块的反馈优化。直接 Codex 获得同一任务、开发数据、方法说明、初始代码和执行权限，可自行检查、规划、编辑、运行和重试。

每个任务每组上限为 **360 秒、400,000 累计输入（含缓存）加输出 token**。主控和所有编码调用共享预算；不比较只扣编码、不扣规划的数字。CLI 的底层逐 HTTP 请求数量不可见，报告为未知，不将一次 CLI 会话称为一次模型请求。两边没有外部实验模型调用。失败、重试、超限及缺失用量都保留；缺失或矛盾用量不认证为预算内成功。

为避免在费用未知时继续消耗，本评测适配器在已启动的 CLI 缺失终结用量后停止整次尝试。这是严格计量模式的停止规则，不是产品原有恢复能力的结论；如果首次编码在这里停止，就没有机会检验后续执行反馈优化。Codex 的内部 rollout token 限额也不能替代最终累计输入加输出审计。

模型是用户当前提供方 `api.sudocode.chat` 的 `gpt-5.6-luna` 别名。Auto Research 主控走 Chat Completions；编码与直接组走 Codex Responses、medium reasoning。别名相同不证明底层权重完全相同，接口与推理设置的差异是比较限制。这不是官方 OpenAI Codex 服务的普遍能力结论。

运行顺序按任务轮换：QASPER before/after/codex，HotpotQA after/codex/before，LoCoMo codex/before/after。每格一次筛查运行，无隐式重试或挑最好结果。不能用 3 个任务证明统计显著的科研能力提升。

## 判分与隔离

基线是固定 BM25，k1=1.2、b=0.75，只使用小写字母数字分词后的正文。每问得分为 top-k 命中证据数 / gold 证据数，再对问题平均。提交必须产生恰好 k 个有效、唯一文档 ID；遗漏、重复、越界预测判无效。

提交包含 `method.py`、可选 Python 辅助模块和 `submission.json`。提交声明选择 baseline 或 method，并报告实际开发集分数。宿主在独立沙箱重新运行冻结代码，只传问题和候选文档，标签始终在宿主。开发和留出复测使用不同的新目录，全部源码只读，运行后再次核对源码集合及哈希。选择 baseline 时不执行提交的辅助模块。

主要成功条件为：按时完成、总 token 合规且计量完整、提交有效、隐藏集绝对提升至少 **0.02**。另报告有效提交率、隐藏集增益、开发声明与复测的差值、错误正向声明、时间、token 和服务失败。保留负结果是合理研究行为，但不等于完成“提升检索性能”目标。

独立评分每个 split 统一最多 120 秒，是所有组相同的外部验收成本，不占自主研究的 360 秒，也不能反馈给 Agent 继续改方法。Windows 原生沙箱启动约需 30 秒，研究中自行启动沙箱的成本计入各自预算。总截止时间会限制正在运行的工具；单次模型请求可能超过剩余 token，超限保留而不算成功。

沙箱复用产品的 Windows 原生进程树限制与网络关闭；拒绝读取原始数据、留出包、日志、其他实验目录和已知私有目录。它仍有原生 root-read 约束，不能描述成完整虚拟机。提供方凭据仅用于宿主模型进程，保存日志前精确脱敏。已证明未启动的进程记零模型调用；已启动却缺失用量的失败记未知。

## 复现

数据准备只需已有公共缓存和带 pyarrow 的 Python；运行、评分本身使用项目 Python 和标准库，不新增依赖。

```powershell
python evals/prepare_short_research.py --output evals/reports/<new-run>/packets
.venv-v3/Scripts/python.exe evals/run_short_research.py --output evals/reports/<new-run>/qasper-before --packets evals/reports/<new-run>/packets --task qasper --arm before
```

按冻结顺序换 task/arm，每次使用新 output。运行器拒绝覆盖旧尝试。公开任务资料会发往当前配置的模型提供方，运行前应确认任务授权和预算。结果、完整源码、收据、控制器状态和计量保存在 output；运行产物由 Git 忽略，协议、生成器、评分器及测试受版本管理。

before 组用 `git show` 读取优化前模块，需要仓库保留 `25cdbc7` 历史。无 Git 历史的纯源码导出可以直接运行 after/codex；完整三组复现应使用原仓库及已保存的冻结输入。

离线验证：`python -m unittest discover -s tests -p test_short_research.py -v`；项目完整验证：`python evals/run_all.py`。数据选择不能在看到 Agent 留出成绩后更改。后续扩大到更多不同任务和独立重复时，应发布新协议版本，而不是重用筛查集作确认性证据。
