# V4 宿主管理实验模型接口

Auto Research 的实验代码可能需要调用模型，但实验沙箱不能拿到项目 `.env`、API key 或网络权限。接口支持预生成 JSONL 批次和依赖前次回答的连续交互，复用同一宿主预算与调用收据。

## 预生成批次

1. Coding Agent 在项目工作目录写入 `model_requests` 指定的输入 JSONL。
2. 宿主读取并严格校验每行 `{id, messages}`，在沙箱外用指定 `model_id` 调用模型。
3. 宿主把 `{id, model_id, content, usage}` 写入输出 JSONL，实验脚本只能读取这个响应文件。
4. 宿主校验输入哈希、输出哈希、响应格式、调用数和时间预算，再启动实验脚本。
5. 结果、模型调用收据、失败原因和恢复状态回到 Auto Research 控制器；研究 LLM 只把有效宿主 measurement 当作实测反馈。

工具参数示例：

```json
{
  "model_requests": {
    "input_path": "requests.jsonl",
    "output_path": "responses.jsonl",
    "model_id": "default",
    "max_requests": 8,
    "seconds": 300
  }
}
```

输入文件每行只能包含 `id` 和 `messages`，不接受 `tool` 消息。输出文件由宿主原子写入；宿主调用中断且没有收据时不会自动重放未知调用。每个 Auto Research 任务默认最多 16 次实验模型调用、1800 秒，可由授权预算收紧或放宽。

## 连续交互

A-MEM 等实验需要先分析记忆，再用实际回答决定演化和问答输入。声明以下合同后，脚本可连续发出请求，不需要提前生成全部 prompt：

```json
{"model_requests":{"mode":"stdio","model_id":"default","max_requests":8,"seconds":300}}
```

```python
import json, sys
print('RESEARCH_MODEL_REQUEST ' + json.dumps({
    'id': 'memory-1', 'messages': [{'role': 'user', 'content': '分析原始对话……'}]
}), flush=True)
response = json.loads(sys.stdin.readline())
# 使用 response['content'] 构造下一次请求，仍通过相同协议发送。
```

响应为 `{id, model_id, content, usage}`。每个命令内 ID 唯一，宿主加命令序号绑定收据；任务内所有命令共用调用数与累计模型时间预算。单行上限 1 MiB（按实际序列化字节计），每次最多32条消息、ID最多200字符，所有消息content合计最多1,024,000个Unicode字符（Python len）；批次输入文件最多8 MiB。完整上下文可放在同一条消息中，不得为通过传输检查静默截断基线输入。消息字段、角色和总长度限制与批次模式相同。普通日志不得使用保留前缀；进程退出仍有未交付请求时，测量失败。连续模式不接受 input_path/output_path。

默认额度仍为 16 次；完整记忆课题可在父研究预算中明确提高，连续模式与父预算上限均为 65,536 次，批次上限仍为 64。该次数计逻辑 complete 调用，供应商重试另有逐次 usage_records，不能当作物理请求数或费用上限。

独立监督线程持续检查取消、实验时限及输出上限，即使宿主模型在途也会关闭沙箱进程树；监督状态读取异常同样停止执行。模型流逐行检查取消与绝对截止时间，覆盖心跳行；无响应期间仍受网络读取时限约束。在途模型调用的收据由原执行线程保存，任务可能等待该调用收尾，不能承诺点击取消后服务商立即停止计费。

完整实验请求与回答只保存在宿主私有 `model-request-NN.json`，以原始内容保证模型数据和恢复一致；状态与公开日志只保留脱敏摘要。通用日志脱敏不得改写实验数据中的普通 `key` 字段。沙箱不能读宿主私有收据或凭据，仅收到本次回复；批次恢复也从收据生成响应。未知的已启动调用或实验不自动重放。

真实接口验收：`evals/run_sequential_model_acceptance.py` 使用 `datasets/project/sequential-model-v1.json` 和 LoCoMo 开发对话的前 8 turns，不读取 QA 标签。2026-09-30 Luna 两次依赖调用通过，18.594 秒、807 个已报告 token，见 [报告](../evals/reports/sequential-model-20260930/luna-r1/report.html)。这是传输和收据验收，未进行 A-MEM 复现或问答质量比较。

该接口解决的是“实验需要模型时如何安全、可追溯地调用”的工程缺口，不代表模型结果本身经过科学验证。实验结果仍必须包含数据集版本、划分、seed、指标和评分来源，并由 baseline/candidate 条件比较决定是否支持目标。


## Windows 实验目录权限生命周期

原生调用通过共用 `run_process` 持有本机文件锁，在运行前记录需要暂时隔离的实验兄弟目录权限。进程树结束后，仅清理本次新增的两种 CodexSandboxUsers read-deny 规则；既有权限、宿主私有目录规则和源码只读保护保持不变。记录位于 `data/native-acl/`。不能拒绝当前实验目录或其祖先，不能通过重解析点修改其他位置。

任务正常结束、失败、取消和超时都走同一清理路径。清理失败保留执行收据并返回 `isolation_cleanup_error`；宿主被强制终止留下未完成记录时，后续原生任务停止，等待核对恢复。该锁仅协调本产品，不覆盖其他独立 Codex 进程。不得在恢复时批量重置历史 ACL；已有规则需核对具体路径及授权范围。


## 待合入：显式实验生成参数（2026-10-02）

[隔离候选](../evals/reports/experiment-generation-20261002/report.md)在上面的每条{id,messages}请求增加可选temperature（0..2有限数值）和max_tokens（1..65536整数），批次与stdio共用校验。省略保持旧行为；显式设置写入HTTP与收据、绑定请求SHA，结束/失败恢复共享客户端设置。提供方拒绝时如实失败，不自动删参或换模型。未经调用的拒绝不继承历史token。

此功能尚未合入主源码或部署，活动r3仍使用旧请求条件。当前比较器尚未核对generation_parameters，因此comparable/goal_met不能证明采样设置一致；未来正式采用须冻结新条件和空缓存。真实Luna组件探测确认0.7/1000被接受并返回READY，46tokens，不能替代原生沙箱或研究质量验收。


2026-10-02 后续[隔离比较候选](../evals/reports/generation-comparison-20261002/report.md)已把宿主记录的生成参数集合纳入LoCoMo合同，影响控制器/V3/验收。历史全缺字段保持原合同；显式/缺失或不同设置不自动配对。比较仅核对观察到的取值集合，不核对频率或提供方行为；额外重试温度可能保守拒绝。仍未部署，不能把历史未知条件当作已核实。
