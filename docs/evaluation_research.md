# H1–H3 评测与 Harness 设计依据

核实日期：2026-09-14。本记录只说明本轮实际读过的来源和取舍，不把设计参考写成项目成绩。

## 固定来源

| 来源 | 论文/项目状态 | 原始链接 | 固定 commit |
|---|---|---|---|
| AgentBench: Evaluating LLMs as Agents | arXiv 2023；正式发表于 ICLR 2024 | [论文](https://arxiv.org/abs/2308.03688) / [仓库](https://github.com/THUDM/AgentBench) | `d1e4a10db08c87075c78972e48ecc182be03e2d5` |
| τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains | arXiv 2024；ICLR 2025 poster | [论文](https://arxiv.org/abs/2406.12045) / [会场](https://iclr.cc/virtual/2025/poster/28170) / [仓库](https://github.com/sierra-research/tau-bench) | `59a200c6d575d595120f1cb70fea53cef0632f6b` |
| AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents | NeurIPS 2024 Datasets and Benchmarks Track | [论文](https://arxiv.org/abs/2406.13352) / [OpenReview](https://openreview.net/forum?id=m1YYAQjO3w) / [仓库](https://github.com/ethz-spylab/agentdojo) | `089ed468cf3ed0322acc66b0211f26d9d90dbf60` |
| Open Deep Research | 开源项目；固定版本未声明对应论文或正式会场 | [仓库](https://github.com/langchain-ai/open_deep_research) | `1b7d2e80db9faa586165c60e09096dbbfd483a64` |
| WikiChat: Stopping the Hallucination of Large Language Model Chatbots by Few-Shot Grounding on Wikipedia | Findings of EMNLP 2023；作为 2024–2026 时间窗外的基础参考 | [论文](https://arxiv.org/abs/2305.14292) / [ACL Anthology](https://aclanthology.org/2023.findings-emnlp.157) / [仓库](https://github.com/stanford-oval/WikiChat) | `803683b1139117d374801688af1d9d968e04a07b` |
| Search-R1 | 两篇 2025 arXiv 预印本，未写成已录用会议成果 | [Search-R1](https://arxiv.org/abs/2503.09516) / [实证研究](https://arxiv.org/abs/2505.15117) / [仓库](https://github.com/PeterGriffinJin/Search-R1) | `598e61bd1d36895726d28a8d06b3a15bed19f5d3` |
| verl / HybridFlow | HybridFlow arXiv 2024；EuroSys 2025 | [论文](https://arxiv.org/abs/2409.19256) / [固定 origin](https://github.com/volcengine/verl) / [当前项目地址](https://github.com/verl-project/verl) | `9e0252efba42a5442bbe52f7d0d2ac68e514581e` |

AgentBench 的会场状态由已保存的一手 arXiv 页面记录 `traces/live/20260913T140652-1d94ed03.jsonl` 核实；τ-bench 的会场状态由 ICLR 官方 poster 页面记录 `traces/live/20260913T141736-afc86d7f.jsonl` 核实。二者的预印本年份与正式会场年份不同。

## 实际阅读与取舍

| 来源 | 实际阅读路径 | 本轮采用 | 本轮未采用及原因 |
|---|---|---|---|
| AgentBench | `README.md` 的 original v0.2、环境与 Dev/Test 描述 | 冻结开发/测试边界、按任务类型分层 | 不接其 Docker 环境、FC/AgentRL 或任务分数；领域和成本不可比 |
| τ-bench | `README.md`、`tau_bench/types.py`、`tau_bench/envs/base.py` | 独立 trial、轨迹、终态与规则分开记录 | 不接模拟用户、业务数据库、输出子串判分；固定 README 已明确 airline/retail 任务过时，且 `pass^k` 不与本项目至少一次成功率混写 |
| AgentDojo | `README.md`、`src/agentdojo/benchmark.py` | 安全与任务质量分开计分、注入对抗样例、上下文溢出单列失败 | 不复制完整攻击/防御 pipeline；`UNTRUSTED_TOOL_DATA` 只是本项目的提示边界，不冒充已证明防御 |
| Open Deep Research | `README.md`、`src/open_deep_research/deep_researcher.py`、`src/open_deep_research/state.py` | research brief 思路、原始材料与压缩视图分开、显式迭代预算 | 不接 LangGraph、多 Agent supervisor、LLM 摘要器或尾部字符截断；H3 用确定性构建器保护协议与 ID |
| WikiChat | `README.md`、`pipelines/chatbot.py`、`benchmark/scripts/evaluate_distillation.py` | Claim/Evidence 映射、相关信息过滤、逐事实引用思路 | 不接 Wikipedia 专用索引、七阶段多次 LLM 调用或 refiner；项目年代超出主检索窗口，只作基础参考 |
| Search-R1 | `README.md`、`search_r1/llm_agent/generation.py` | 可纠正动作错误、轮次/Prompt/Observation 预算、最后一轮禁用 search | 不接 RL、GPU rollout、reward mask，也不采用 `input_ids[:, -max_len:]` 尾截断，因为会破坏问题或工具配对 |
| verl | `README.md`、`verl/experimental/agent_loop/tool_agent_loop.py` | 显式状态、工具名/JSON 校验、错误反馈、`tool_call_id` 和响应上限 | 不接分布式 RL、logprob/reward、GPU 或多模态基础设施；均超出 H1–H3 |

固定克隆均为 shallow、promisor、`blob:none`；只把上表列出的 blob 视为实际阅读证据。固定 origin 仍是 `volcengine/verl`，但其 README 已说明 2026 年迁移至 `verl-project/verl`，所以两者同时记录。

SWE-bench、OSWorld、Harness-Bench 和 `awesome-agent-harness` 只作为背景索引。本轮没有固定并阅读与上表同等级的代码版本，因此没有据此声称实现移植。

## 与 H1–H3 的对应关系

```text
AgentBench / τ-bench / AgentDojo
    -> H1 数据边界、重复运行、质量/安全/工程失败分开
Search-R1 / verl
    -> H2 错误反馈、动作协议、final-only 与显式预算
Open Deep Research / WikiChat
    -> H3 原文/摘要分离、结构化状态、Claim-Evidence 映射
```

本轮只采用能由标准库和现有显式 Loop 支撑的最小机制。没有加入第三方 Agent 框架、向量库、LLM 上下文压缩、多 Agent、训练环境或 RL。

## 当前指标口径

- `task_success`：只聚合有版本化答案/Claim 标注的运行；未标注保持 `UNJUDGED`。
- `answer_correctness`：答案事实是否正确，与引用 ID 是否存在分开。
- `claim_support`：标注的 Claim 是否被指定 Evidence 支持；H4 前不把 lexical verifier 当语义真值。
- `citation_id_validity`：按最终答案中引用出现次数统计已知/未知 S/E ID，不等于事实支持。
- `mean_single_run_rate / at_least_once_rate / all_runs_rate`：同一任务三次独立运行的三种聚合，不拼接不同配置。
- `tool_attempts / tool_calls / network_requests / tool_denials / tool_successes`：分别记录模型动作、逻辑执行、HTTP 成本、拒绝和成功。
- `estimated_input_tokens`：H3 使用公开的 UTF-8 bytes / 3 保守估算和安全余量，不声称是 tokenizer 精确值。

## 为什么不直接报告外部 Benchmark 分数

这些项目的任务环境、答案标准、模型预算和安全目标与技术资料研究 Agent 不同。直接引用其分数，或把本仓库 7 条 fixture 的满分写成通用模型效果，都会误导。本项目因此先用固定探针验证 Harness 行为，再用冻结 dev/test 和版本化人工/Judge 标注测真实质量。

本轮真实 dev C 尝试因 Windows `WinError 10013` 在首个模型请求失败，150/150 均为工程失败；它没有被 fixture 替代，也没有被写成质量收益。成功的真实 A–D 对照仍待网络权限和标注条件满足后执行。

## 当前结论

H1、H2、H3 的确定性专项门禁分别证明判分校准、安全/恢复行为和上下文成本相对各自冻结基线改善。它们不证明真实模型答案质量已经提高。项目现处于 `PAUSED_FOR_RESEARCHAGENT`；H4 语义反馈、长期记忆和 Agent-RL 均未开始。
