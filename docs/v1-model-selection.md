# V1：对话模型切换

## 问题

原来所有对话和研究只使用 `.env` 中的一组模型配置。现在在输入框上方显示当前模型，并提供下拉切换：原有配置（本机为 DeepSeek / deepseek-flash）及 Sudocode 的 GPT 5.6 Luna、Terra、Sol。

## 配置

保留现有 `PROVIDER`、`BASE_URL`、`API_KEY`、`MODEL` 和检索配置。在项目根目录 `.env` 中追加：

```dotenv
SUDOCODE_BASE_URL=https://api.sudocode.chat/v1
SUDOCODE_API_KEY=填写你的中转站密钥
SUDOCODE_LUNA_MODEL=gpt-5.6-luna
SUDOCODE_TERRA_MODEL=gpt-5.6-terra
SUDOCODE_SOL_MODEL=gpt-5.6-sol
SUDOCODE_TIMEOUT_SECONDS=180
```

除密钥外其余字段均有上述默认值。模型 ID 必须与 Sudocode 账户实际开放的名称一致；如其名称不同，修改对应的 `SUDOCODE_*_MODEL`，无需改代码。三种模型通过 `/v1/chat/completions` 使用文本和 search/read 工具调用。针对 Sudocode 不发送固定 temperature 参数，由该服务使用默认采样设置。

配置后重启服务并刷新页面。密钥为空时三个选项显示“未配置”且不可选；接口直接提交未配置模型也会明确报错，不会回退到 DeepSeek 或演示回复。离线模式仅允许固定演示模型。

## 工作原理

- `GET /api/models` 返回模型 ID、显示名称、实际请求模型名称和是否已配置，不返回密钥。
- 消息接口支持 `model_id`：`default`、`sudocode-luna`、`sudocode-terra`、`sudocode-sol`。旧客户端不传时沿用原有配置。
- 同一条消息的意图路由和后台研究使用同一个所选模型配置。每次创建独立客户端，切换不会修改共享的环境变量。
- 浏览器保留最后选择；模型选择影响之后发送的消息。任务入队时保存 `model_id/model_name`，切换不会改写排队中或正在执行的任务。取消后的重试继续使用原任务模型配置。
- 回复、任务详情和报告标注所选模型。历史数据迁移默认保留为原配置，模型名称为空时不猜测过去实际用了哪个模型。
- Sudocode 只读取 `SUDOCODE_API_KEY`，不会借用原有 DeepSeek 密钥。凭据不保存在浏览器、消息表或任务表。

模型别名映射和凭据由启动配置管理。修改 `.env` 中的映射后重启，后续请求/重试使用新的映射；数据库只保存名称与配置 ID，不保存旧凭据。

## 验证（2026-09-16）

- 123 项单元测试通过；Stage 1 为 4/4、离线 Harness 为 21/21、Stage 3–7 通过；前端脚本语法检查通过。
- 新增回归覆盖：两个服务凭据隔离、公开列表不含密钥、自定义模型 ID、未知/未配置模型拒绝且不写入消息、历史数据库迁移、HTTP 消息选择、不同模型交错入队、重试保留模型及旧客户端兼容。
- 模拟兼容服务验证真实客户端的请求地址、Authorization、模型名以及搜索工具调用；不是对 Sudocode 的真实联网验证。
- 浏览器独立测试：Terra 聊天、Sol 后台研究、Luna 聊天都保留对应模型标记；刷新保留 Luna；既有研究仍显示 Sol；页面无脚本错误。

首轮实现时用户尚未提供 Sudocode 密钥，因此上述测试未验证该账户实际支持的模型 ID、接口响应、额度或工具调用能力。

## 403 / 1010 修复（2026-09-16）

用户填入密钥后，原请求返回 `HTTP 403: error code: 1010`。这发生在路由请求阶段，尚未创建研究任务。原实现已经使用 OpenAI 的 Chat Completions JSON 格式；本次补充 `User-Agent: ResearchAgent/1.0` 和 `Accept: application/json` 后，相同中转站的 `/v1/models` 和 Luna 最小文本请求均返回 200。模型列表确认包含 `gpt-5.6-luna`、`gpt-5.6-terra`、`gpt-5.6-sol`。

保留 `/v1/chat/completions`、`messages` 和 `tools` 接口；不把尚未发生的协议兼容错误当作原因去改换接口。后续如再次遇到 403 / 1010，错误信息明确指出网关拒绝，而不是“模型拒绝回答”。

首轮完整研究复测还发现：第一次搜索成功后，第二次模型调用超过原固定 45 秒而超时。Sudocode 现在默认单次请求等待上限为 180 秒，可用 `SUDOCODE_TIMEOUT_SECONDS` 调整（正有限数）；不需要修改已有 `.env` 即可使用新默认值。其他提供方仍沿用原时限。模型调用失败时保留脱敏后的具体错误，避免被“来源没有有效引用”覆盖；真实失败仍记为失败，不自动切换模型。

第二轮实连已完成搜索、arXiv 原文阅读和补充检索，但上下文压缩后模型再次请求已读来源，触发旧的重复动作硬停止。现在不再次访问网络，而是回送已有工具结果，再给模型一轮总结机会；该轮禁止继续调用工具。持续重复的模型仍以 `duplicate_action` 停止，不形成无限循环。回归验证覆盖缓存证据、只发一次网络工具调用、可正常完成引用回答，以及持续重复时的有界停止。

[OpenAI 官方迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)区分了 Chat Completions 和 Responses 两种格式；本中转站实际接受当前 Chat Completions 格式。可运行 `python -B evals/run_sudocode_smoke.py --output evals/reports/sudocode-new.json` 验证三个真实模型的工具调用往返及一次独立研究；模型名和密钥仍从 `.env` 读取。工具协议探针使用合成的工具结果，随后 Luna 的研究案例才使用真实搜索和网页阅读。

### 本轮最终验证

- `python -B evals/run_all.py`：127 项单元测试通过；Stage 1 为 4/4，离线 Harness 为 21/21，Stage 3–7 通过。
- [真实验收报告](../evals/reports/sudocode-live-verified-20260916.json)：4/4。Luna、Terra、Sol 的原生工具调用往返全部通过；Luna 的隐式论文提问自动进入研究，经两次搜索、一次 arXiv HTML 原文读取，最终生成带有效 S/E 引用的回答，状态 `completed / model_final`。
- 原文读取存在截断；最终回答区分已核实预印本和未独立核实的 COLM 发表信息。这是接口和工作流验收，未对事实正确率进行独立量化，不能推出所有问题都可靠。
- 保留两次失败报告及各自 Trace：[45 秒超时](../evals/reports/sudocode-live-20260916.json)、[重复动作停止](../evals/reports/sudocode-live-final-20260916.json)。每份报告记录执行前代码哈希，不覆盖失败样本。
- 测试使用独立数据库，未向用户原有会话加入测试消息；部署不改写 `.env`。
