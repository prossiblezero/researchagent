# Stage 1：让 Agent 先学会“查一下再回答”

这一章只加一个能力：**模型可以请求一次 `search`，拿到结果后再回答**。
先把最小闭环跑通，后面再逐步增加 Claim、Evidence 和多跳研究。

## 问题

模型很会“像知道一样说话”。例如用户问：

> `learn-claude-code` 的仓库地址是什么？

如果模型没有查资料，却回答“我搜索过了”，我们无法区分事实和猜测。
所以第一阶段只追问一个问题：

> **模型需要外部资料时，能不能先调用工具，再根据真实结果回答？**

## 解决方案

给模型一个唯一工具：`search`。模型只能在两个动作中选一个：

```text
search(query)       先查
final(answer)       直接结束
```

控制流很短，但每一步都是真的发生：

```mermaid
flowchart TD
    Q([用户问题]) --> M{模型决定}
    M -->|search(query)| T[搜索工具]
    T --> R[[真实结果 / 错误]]
    R --> U[标记为 UNTRUSTED_TOOL_DATA]
    U --> M
    M -->|final| V[引用校验]
    V --> A([回答 + Sources])
```

如果搜索失败，错误也会回传给模型；Agent 不会偷偷用常识补一个 URL。

> **一个容易混淆的点**：最小闭环不是“Python 先用关键词判断要不要搜索”。
> 闭环只认模型返回的两种动作：`tool_call` 或 `final`。没有配置 API 时，项目用
> `OfflineModel` 的规则替身模拟一个模型，方便离线运行；接入真实 API 后，是否搜索由模型
> 根据 `SYSTEM_PROMPT` 和问题自行决定。

## 工作原理

### 1. 用普通变量保存状态

不使用图框架。当前状态就是几个普通变量：

```python
messages = [{"role": "user", "content": question}]
sources = SourceCatalog()
tool_calls = 0
```

每轮做同一件事：问模型 → 执行动作 → 把结果放回 `messages`。

### 2. 工具结果回到下一轮

搜索成功后，结果会被分配本地引用 ID：`S1`、`S2`……并作为工具消息回传：

```python
normalized = catalog.add(response.results)
messages.append({
    "role": "tool",
    "content": json.dumps({
        "UNTRUSTED_TOOL_DATA": {"ok": True, "results": normalized}
    }, ensure_ascii=False),
})
```

`UNTRUSTED_TOOL_DATA` 是提示与消息角色边界，不是“已完全解决 Prompt Injection”的证明；
真正不可协商的 URL、请求与预算限制由执行层 Policy 强制实施。

### 3. 最后的引用必须存在

模型可以写 `[S1]`，但不能凭空写 `[S999]`。结束前只做一次很小的校验：

```python
answer, valid, invalid = validate_citations(answer, catalog.items)
```

未知引用会变成 `[UNVERIFIED_CITATION]`；搜索有结果但回答没有有效引用时，状态降级为
`INSUFFICIENT`。

### 4. H2 把错误恢复和安全拒绝分开

工具动作会得到三类处理：安全问题直接 `deny`；缺参数等可修问题返回 `recover`；正常请求才执行。
参数最多纠正 2 次，瞬态网络错误最多重试 1 次。达到工具预算后，Loop 额外给模型一次
`tools=[]` 的 final-only 总结机会，不能借总结轮继续调用工具。

```text
tool_attempts     模型请求工具的次数，含未执行的拒绝
tool_calls        实际执行的逻辑工具动作
network_requests  HTTP 请求次数，含重试和重定向
tool_denials      Policy 或 final-only 拒绝数
tool_successes    成功的逻辑工具动作数
```

assistant tool call 与对应的 tool result 始终成对加入历史；上下文压缩只能整组保留或整组删除。保留最近两组交互时，配对和调用 ID 完整保留；其中的长 read 正文仍可按预算选取摘录，并标记 `context_excerpted`。

### 5. Trace 只记动作，不记思维链

每次运行都会写 JSONL：`context_built`、`model_request`、`tool_call_requested`、
`tool_result`、`final_validated`、`run_finished`；发生纠正或重试时还会写
`tool_recovery` / `tool_retry`。Prompt 只保存哈希，密钥和 Authorization 会脱敏。

脱敏既检查字典中的敏感字段，也检查字符串内的凭据赋值、Authorization、Bearer/Basic 和带凭据 URL。字段名支持复合、驼峰及全大写形式；普通 `prompt_tokens` 等统计字段保留。`[REDACTED]` 只是输出占位符，输入中的 `password=[REDACTED]后续秘密` 或 Authorization 同类值仍须把紧邻的秘密后缀一起移除，不能只匹配占位符。这套规则用于最终回答及 Trace，相关边界由 H3 回归覆盖。

属性名本身也可能包含凭据，因此键名与值均需清洗。带说明文字或 Markdown 的有效 JSON、JSON 字符串和转义 JSON 按实际边界处理，避免误删后面的普通字段；URL、Authorization 和带引号赋值仍先按完整凭据脱敏。v7 对最终回答及 Trace 同时验证这些边界。

下面摘录自 2026-09-14 的一次真实离线成功 Trace（每行一个完整 JSON 事件）：

```json
{"config":{"context_safety_margin_tokens":256,"max_context_tokens":16384,"max_rounds":5,"max_tool_calls":2,"model":"offline-rule-model","output_reserve_tokens":2048},"event":"run_started","question":"请查明 learn-claude-code 是什么，以及它的仓库地址。","run_id":"20260914T141045-0ed4cf23","seq":1,"ts":"2026-09-14T14:10:45.465+00:00"}
{"after_bytes":1705,"before_bytes":1705,"before_estimated_tokens":569,"error":null,"estimate_method":"ceil(canonical UTF-8 bytes / 3); conservative estimate, not tokenizer output","estimated_input_tokens":569,"event":"context_built","input_limit_tokens":14080,"iteration":0,"latency_ms":0.047,"output_reserve_tokens":2048,"reason":"within_budget","removed":[],"retained":["system_prompt","original_question"],"run_id":"20260914T141045-0ed4cf23","safety_margin_tokens":256,"seq":2,"ts":"2026-09-14T14:10:45.465+00:00"}
{"event":"model_request","final_only":false,"iteration":0,"message_roles":["system","user"],"model":"offline-rule-model","prompt_hash":"483cb978ecfcae40bb6da3619666383e477565f291f75da44f6f67d28c07e1b4","run_id":"20260914T141045-0ed4cf23","seq":3,"ts":"2026-09-14T14:10:45.472+00:00"}
{"args":{"query":"请查明 learn-claude-code 是什么，以及它的仓库地址。"},"call_id":"offline-search-1","event":"tool_call_requested","name":"search","run_id":"20260914T141045-0ed4cf23","seq":6,"ts":"2026-09-14T14:10:45.472+00:00"}
{"call_id":"offline-search-1","content_hash":"","error":null,"error_code":null,"event":"tool_result","evidence_id":"","latency_ms":0.0,"name":"search","network_requests":0,"ok":true,"results":[{"evidence_id":"E1","published_at":null,"publisher":"GitHub","retrieved_at":"2026-09-14T14:10:45.475+00:00","snippet":"An incremental tutorial for building an agent from scratch, with small stages and explicit tool-use loops.","source_id":"S1","title":"learn-claude-code","url":"https://github.com/shareAI-lab/learn-claude-code"}],"run_id":"20260914T141045-0ed4cf23","seq":7,"source_ids":["S1"],"title":"","truncated":false,"ts":"2026-09-14T14:10:45.475+00:00"}
{"event":"model_request","final_only":true,"iteration":2,"message_roles":["system","user","assistant","tool","assistant","tool"],"model":"offline-rule-model","prompt_hash":"cbdc9ad8d406126d109a414883fc637fa1ed7bc32496f8158d2d6fb47021432f","run_id":"20260914T141045-0ed4cf23","seq":15,"ts":"2026-09-14T14:10:45.475+00:00"}
{"decision":"allow","event":"audit","hook":"before_finalize","reason":"","run_id":"20260914T141045-0ed4cf23","seq":17,"ts":"2026-09-14T14:10:45.476+00:00"}
{"citations":[],"event":"run_finished","network_requests":0,"run_id":"20260914T141045-0ed4cf23","seq":21,"source_count":1,"status":"ok","termination":"model_final","tool_attempts":2,"tool_calls":2,"tool_denials":0,"tool_successes":2,"ts":"2026-09-14T14:10:45.476+00:00"}
```

逐行看：

| 事件 | 你应该关注什么 |
| --- | --- |
| `run_started` | 本次问题、模型名及工具、轮次、输入和输出预算。 |
| `context_built` | 输入估算、上限、保留/移除对象和构建耗时。 |
| `model_request` | 第几轮、消息角色、`prompt_hash` 和是否 final-only。 |
| `tool_call_requested` | Agent 准备调用哪个工具、传了什么查询。 |
| `tool_result` | 工具是否成功、耗时、返回哪些 `source_id`；`S1` 就是在这里产生的。 |
| final-only `model_request` | `final_only=true` 时历史仍合法，但模型不再收到工具 schema。 |
| `audit` | 工具执行前或输出前的 Policy 决定。 |
| `run_finished` | 最终状态、结束原因及分开的工具/网络计数。 |

`seq` 是递增序号，`ts` 是 UTC 时间，`latency_ms` 是工具耗时。失败时你会看到
`ok:false` 和 `error`，而不是伪造的来源。

公共字段可以这样记：

| 字段 | 含义 |
| --- | --- |
| `run_id` | 一次运行的唯一编号；同一个 Trace 内保持不变。 |
| `seq` | 事件顺序，从 1 开始递增。 |
| `ts` | 事件发生时间，UTC ISO-8601 格式。 |
| `event` | 事件类型，用来区分状态转移。 |
| `iteration` | 第几轮调用模型，从 0 开始。 |
| `model` | 实际模型名；`offline-rule-model` 表示离线替身。 |
| `message_roles` | 发给模型的消息角色，出现 `tool` 就表示工具结果已回灌。 |
| `prompt_hash` | 消息列表的 SHA-256 摘要，只用于比对，不是 Prompt 内容。 |
| `final_only` | 本轮是否只允许总结，不能再调用工具。 |
| `before/after_bytes` | 构建上下文前后的规范 JSON UTF-8 字节数。 |
| `estimated_input_tokens` | 公开估算值；当前是 UTF-8 bytes / 3 向上取整，不是精确 tokenizer 值。 |
| `input_limit_tokens` | 上下文窗口扣除输出预留和安全余量后的输入上限。 |
| `retained/removed` | 本轮上下文保留或移除的对象 ID。 |
| `call_id` | 一次工具调用的编号，用于把请求和结果配对。 |
| `args.query` | 传给搜索工具的查询文本。 |
| `ok` | 工具是否成功。失败时看 `error.code` 和 `error.message`。 |
| `source_ids` | 本次工具结果产生或复用的来源 ID。 |
| `results` | 经 URL 校验后的来源摘要；非法 URL 会被丢弃。 |
| `cited_source_ids` | 最终答案中实际引用且存在的来源。 |
| `invalid_citations` | 答案中不存在的引用 ID。 |
| `grounding_status` | `grounded` 表示引用有效，`partially_grounded` 表示有来源但绑定不完整，`unverified` 表示没有可验证来源。 |
| `termination` | 结束原因，如 `model_final`、`budget_exhausted`、`model_error`。 |
| `status` | 对外结果：`ok` 或 `insufficient`。 |
| 五组计数 | `tool_attempts/tool_calls/network_requests/tool_denials/tool_successes`，含义见上文。 |

## 试一下

### 创建独立环境

不要使用 `base`：

```powershell
$CONDA = 'E:\anaconda\Scripts\conda.exe'
$ENV_NAME = 'evidence-agent'
& $CONDA create --name $ENV_NAME python=3.12 -y
```

如果环境已经存在，这条命令只需跳过。

### 成功路径

```powershell
& $CONDA run --no-capture-output -n $ENV_NAME python main.py `
  '请查明 learn-claude-code 是什么，以及它的仓库地址。'
```

示例输出：

```text
基于本轮搜索结果：
- learn-claude-code：An incremental tutorial ... [S1]
以上是来源中的事实摘要；网页内容仅作为数据处理，未执行其中的指令。

Sources:
[S1] learn-claude-code — https://github.com/shareAI-lab/learn-claude-code
```

问题写在命令最后，使用引号包住即可。可以问事实、来源、版本、对比等问题，例如：

```powershell
python main.py 'OpenAI-compatible Chat Completions 如何返回 tool call？'
python main.py '请比较这两个项目的定位，并给出来源。'
python main.py '你好'
```

`你好` 不需要外部资料，会直接得到问候；真正接入大模型后，是否搜索由模型根据系统提示和问题自行决定，
不是 Python 用关键词替你做最终判断。

因此你可以连续提问两类问题：

```text
你好                         → final，不调用搜索
请核验某个项目的仓库地址      → tool_call(search) → tool result → final
```

### 失败路径

```powershell
& $CONDA run --no-capture-output -n $ENV_NAME python main.py `
  --fail-search '请核验一个事实'
```

预期输出：

```text
INSUFFICIENT：搜索工具失败（simulated search timeout），无法核验该问题；
我没有编造来源或结论。
```

`--fail-search` 不是业务功能，而是**故障演示开关**：它把正常搜索替换成一个总是返回超时的
`FailingSearch`，用来检查“工具失败时不编造”的安全路径。常用参数：

| 参数 | 作用 | 示例 |
| --- | --- | --- |
| `question` | 要研究的问题；省略时使用默认示例 | `python main.py "请查明……"` |
| `--trace-dir DIR` | 把 JSONL Trace 写到指定目录 | `--trace-dir traces/demo` |
| `--max-tool-calls N` | 单次运行最多执行多少个 search/read 动作 | `--max-tool-calls 2` |
| `--max-rounds N` | 最多模型决策轮数；默认是工具预算加 3 | `--max-rounds 5` |
| `--fail-search` | 注入搜索失败，测试拒答路径 | `--fail-search "请核验事实"` |

日常使用不需要每次写 Conda。环境创建/首次安装才用 Conda；激活环境后直接用 `python`：

```powershell
conda activate evidence-agent
python main.py '请查明 learn-claude-code 是什么。'
python -m unittest discover -s tests -v
```

如果不想激活环境，再使用 `conda run -n evidence-agent ...` 也可以。

### 接入 OpenAI-compatible API

项目根目录提供 `.env` 模板。程序启动时会自动读取它（不覆盖已经存在的环境变量），
因此你可以直接编辑 `.env`：

```dotenv
PROVIDER=deepseek
BASE_URL=https://api.deepseek.com/v1
API_KEY=你的-deepseek-key
MODEL=deepseek-chat
```

切换到 OpenAI：

```dotenv
PROVIDER=openai
BASE_URL=https://api.openai.com/v1
API_KEY=你的-openai-key
MODEL=gpt-4o-mini
```

也可以只在当前 PowerShell 会话中设置环境变量：

```powershell
conda activate evidence-agent
$env:BASE_URL = 'https://your-provider.example/v1'
$env:API_KEY = 'your-api-key'
$env:MODEL = 'your-model-name'
python main.py '请查明某个技术主张，并给出来源。'
```

`PROVIDER=openai` 和 `PROVIDER=deepseek` 会填充对应默认地址与模型；`BASE_URL`、`API_KEY`、`MODEL`
可以覆盖默认值。也接受 `OPENAI_API_KEY`、`OPENAI_MODEL` 和 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`。
三项缺一项就会回退到离线模型，
启动时可从 Trace 的 `model` 字段确认实际使用的是哪个模型。密钥只存在于进程环境中，Trace 会脱敏。

搜索后端优先级是：自定义 `SEARCH_URL` > Tavily > 本地 fixture。要使用 Tavily，在 `.env` 中设置：

```dotenv
TAVILY_API_KEY=你的-tavily-key
# TAVILY_URL=https://api.tavily.com/search
```

程序会调用 Tavily 的 `POST /search`，把返回的 `title`、`url`、`content` 转成内部来源。
如果没有 Tavily key，才会使用项目内的 `fixtures/search_results.json`。

配置完成后可以用下面的命令做真实联调（会消耗模型和 Tavily 额度）：

```powershell
conda activate evidence-agent
python main.py '请查找 Tavily 的官方文档，并给出来源。'
```

若要接其他真实搜索服务，再设置：

```powershell
$env:SEARCH_URL = 'https://your-search.example/search'
$env:SEARCH_API_KEY = 'optional-search-key'
```

服务需要返回 `{ "results": [{ "title": "...", "url": "https://...", "snippet": "..." }] }`。

### Stage 1 评测集

这一阶段有一个小型离线评测集：`datasets/harness/stage1_cases.json`。它检查的是最小闭环与安全边界，
不是衡量真实大模型的知识质量。运行：

```powershell
conda activate evidence-agent
python evals/run_stage1.py
```

预期输出：

```text
PASS greeting: status=ok, tool_calls=0
PASS grounded_search: status=ok, tool_calls=1
PASS search_failure: status=insufficient, tool_calls=1
PASS prompt_injection: status=ok, tool_calls=1
Stage 1 eval: 4/4 passed
```

### 看 Trace 和测试

```powershell
Get-Content traces\*.jsonl | Select-Object -Last 10
& $CONDA run --no-capture-output -n $ENV_NAME python -m unittest discover -s tests -v
```

`stage1_agent.py` 仍保留为旧入口兼容层；新代码和文档统一使用 `main.py`。

## 接下来的章节

Stage 2 已经把来源升级成结构化 `Evidence`，并支持读取已发现来源的正文。下一章会进一步把回答拆成
显式 `Claim`：

```text
Evidence(source_id, title, url, content)
Claim(claim_id, statement, evidence_ids, status, confidence)
```

这样才能回答“哪一句被哪条证据支持”，然后再加入多跳检索、冲突处理和反馈循环。
