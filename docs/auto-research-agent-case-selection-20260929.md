# 较新 Agent 论文案例：A-MEM 候选（2026-09-29）

选择建议：在当前 SciFact 方法迭代流程完成后，优先用 A-MEM 的公开复现代码验证真正的 Agent 记忆研究案例。它与现有多会话记忆、原文溯源和检索能力相关，能覆盖记忆构建、更新、检索和最终问答；本页只是基于源码的适配准备，尚未复现，不是新实验成绩或创新结论。

## 已核对来源

- 系统仓库[agiresearch/A-mem](https://github.com/agiresearch/A-mem)明确把论文复现指向另一仓库，不能只跑安装示例就声称复现论文。
- [论文复现仓库](https://github.com/WujiangXu/AgenticMemory/tree/0c8039f28fdcc08189a23c07a3437d9d2482f9c2)锁定提交 `0c8039f28fdcc08189a23c07a3437d9d2482f9c2`（本次git ls-remote读取HEAD）。README给出的题目为“A-MEM: Agentic Memory for LLM Agents”，引用标为NeurIPS 2025；发表信息尚需会议官网与[论文原文](https://arxiv.org/pdf/2502.12110)进一步核对，不能仅以README认证发表状态。
- 已逐段检查 `test_advanced_robust.py` 的记忆导入、问答及指标路径，并检查 `memory_layer_robust.py` 的模型适配接口及 `utils.py`、`load_dataset.py` 的相关入口。原文件和MIT许可下载至 `D:/paper/researchagent-sources/AgenticMemory/<commit>/`，未执行上游代码或安装依赖。
- GitHub API返回403限流；改用公开git远端引用和固定提交raw文件读取，未把接口失败当仓库不可用。

## 与当前执行基础的真实差距

1. 上游会对每个对话turn生成/演化记忆，并在问答时调用模型；当前实验沙箱禁网络且不继承模型凭据。因此不能直接让Codex运行上游后便宣称接通。需要一个宿主控制的实验模型调用接口，复用现有模型设置、预算、用量和收据，实验代码只提交输入、接收响应；未知请求不自动重放。不得把.env或key交给实验代码，或为跑通而开放任意网络。
2. `requirements.txt`没有固定版本；sentence-transformers/torch/transformers及多种评分器有额外权重依赖。实验前固定最小必要版本并在D:/paper缓存；核对当前原生进程树2GiB内存上限是否适用，资源不足应有明确结果。不能用“不运行embedding或记忆演化”替代完整baseline。
3. 上游 `--ratio` 选择前若干完整conversation，并非随机抽取相同比例QA。需要在datasets中预先冻结conversation与QA ID、dev/终验划分及模型条件，结果报告实际对话和题目数。
4. 上游类别5将参考答案作为选择项之一，属于特定选择题协议；不能与普通开放问答混为一项准确率。优先复现类别1–4，类别5另列协议或明确排除原因。逐样本预测和标准答案分开保存，预测接口不接收标准答案。
5. 原代码以样本序号命名pickle记忆缓存。受控实验只使用本次自行生成的缓存，绑定原始对话hash、代码、模型及参数，避免不同数据/方法复用旧缓存。不能载入来源不明pickle。

## 下一例的完成标准

由研究Agent阅读原文及相关方法、保存baseline复现计划、自主委派Codex接入固定公开实现，运行真实记忆构建和问答。然后根据失败样本提出有来源的待验证方法变化，保留独立的对比和相应消融，在固定多个conversation及两个预先分离的评测集合上记录答案指标、证据/历史召回、模型请求和总token、构建/问答耗时及失败。

可研究的方向是记忆演化时如何保留原始事实和时间来源、如何处理冲突、如何控制更新成本；这些只是待原文调研的新颖性候选，当前不写成三项已成立贡献。先复现上游行为，再由实际结果决定修改方向，不预设必须获得正增益。

这一步与自动研究总体成果归档、真实中断恢复、木屋交付一样仍在完整目标范围内；SciFact的小型迭代验收不会替代它。

## 2026-09-30 连续接口与权重准备

CodingTool 已补充连续 stdio 模型协议，见 [接口说明](v4-host-model-interface.md)。Luna 在原生沙箱用 LoCoMo 开发对话前8 turns完成“生成记忆→依据实际记忆回答”两次依赖调用，完整收据、807 token和18.594秒记录已归档。这是必要接口验收，未执行官方 A-MEM 算法或80题面板。

官方代码所用 `sentence-transformers/all-MiniLM-L6-v2` 已固定 Hugging Face revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`，11项权重/配置文件下载到 `D:/paper/researchagent-models/all-MiniLM-L6-v2/<revision>/`，逐文件URL和SHA-256保存在 `download-manifest.json`。没有加载外部pickle或修改主Python环境。

主环境已有 sentence-transformers 6.0.1、torch 2.14.0、transformers 5.17.0；仍需在独立实验环境准备 nltk、rank-bm25、litellm、openai及所选评分依赖。上游robust入口导入时会下载NLTK资源并加载模型，适配时须提前准备到D:/paper、使用本地权重并保留原算法；不得因禁网而删去embedding、记忆演化或邻居更新。官方baseline应明确命名所用robust版本，模型与采样设置差异如实记录。
