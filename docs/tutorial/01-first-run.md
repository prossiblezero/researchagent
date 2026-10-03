# 01｜跑通最小闭环：先看到行为，再解释原理

> 最小实验要能重复，也要能看到失败。

[上一章](00-project-map.md) · [课程首页](README.md) · [下一章](02-agent-loop.md)

## 问题：本机已有模型配置，怎样学代码而不意外发起真实研究？

`main.py` 会加载根目录 `.env`。因此，教学时显式设置 `OFFLINE_MODE=1`，让模型与搜索使用固定数据。离线回答验证控制流，不能代表真实模型质量。

以下命令在 **新的 PowerShell 窗口**中执行，工作目录为项目根目录。使用当前已有 `.venv-v3`；环境变量只影响该窗口及其子进程，结束后关闭窗口即可。

## 第一步：一次问候与一次研究

```powershell
Set-Location D:\project\researchagent
$tutorialPython = Join-Path (Get-Location) '.venv-v3\Scripts\python.exe'
$tutorialRun = Join-Path $env:TEMP ('researchagent-tutorial-' + [guid]::NewGuid().ToString('N'))
$env:OFFLINE_MODE = '1'
$env:PYTHONUTF8 = '1'
$env:RETRIEVAL_MODE = 'lexical'

& $tutorialPython -B main.py '你好' --trace-dir $tutorialRun
& $tutorialPython -B main.py '请查明 learn-claude-code 是什么，以及它的仓库地址。' --trace-dir $tutorialRun
```

预期观察：问候不调用搜索；研究问题返回固定来源和引用，并输出 Trace 路径。Trace 写入独立临时目录。CLI 本身不把这次结果写入工作台数据库。

如果 `.venv-v3` 不存在，基础 CLI 可先用已有 Python 3.12 执行 `python -B main.py ...`；完整环境的依赖见 [requirements.txt](../../requirements.txt)。密集检索需要另外准备模型，见[检索环境说明](../context-memory.md)和[准备脚本](../../setup_retrieval.py)，入门闭环不需要下载权重。

## 第二步：主动制造搜索失败

```powershell
& $tutorialPython -B main.py --fail-search '请核验一个事实' --trace-dir $tutorialRun
& $tutorialPython -B evals/run_stage1.py
```

`FailingSearch` 给出确定性失败；Agent 应如实交付证据不足。`run_stage1.py` 会检查问候、成功搜索、失败和注入等固定行为。这里“程序没有崩溃”和“问题得到完整解答”是两个不同条件。

## 第三步：读取刚才的 Trace

```powershell
$tutorialTrace = Get-ChildItem -LiteralPath $tutorialRun -Filter '*.jsonl' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-Content -LiteralPath $tutorialTrace.FullName |
    ForEach-Object { $_ | ConvertFrom-Json } |
    Select-Object seq,event
```

重点寻找这些事件，而不是背整份日志：

| 事件 | 帮你回答的问题 |
| --- | --- |
| `run_started` | 问题、模型和预算是什么？ |
| `model_request` / `model_response` | 模型在哪一轮做了什么类型的决策？ |
| `tool_call_requested` / `tool_result` | 有没有真正执行工具？结果成功还是失败？ |
| `final_validated` | 引用是否合法？ |
| `run_finished` | 为什么停止？用了多少工具？ |

是否出现阅读、补查等事件取决于当前路径。离线模型按预设逻辑生成结果，不能用这次演示宣称“模型自主读懂了一篇论文”。

## 第四步：用隔离数据库启动教学工作台

在另一个新的 PowerShell 窗口运行以下完整命令。它使用独立端口、数据库和 Trace，不复用日常研究的运行数据。

```powershell
Set-Location D:\project\researchagent
$tutorialPython = Join-Path (Get-Location) '.venv-v3\Scripts\python.exe'
$tutorialWebRoot = Join-Path $env:TEMP ('researchagent-web-' + [guid]::NewGuid().ToString('N'))
$env:OFFLINE_MODE = '1'
$env:PYTHONUTF8 = '1'
$env:RETRIEVAL_MODE = 'lexical'
$env:RESEARCHAGENT_TUTORIAL_ROOT = $tutorialWebRoot

@'
import os
from pathlib import Path
from server import make_server

root = Path(os.environ['RESEARCHAGENT_TUTORIAL_ROOT'])
server = make_server('127.0.0.1', 8020,
                     db_path=root / 'tutorial.db', trace_dir=root / 'traces')
print('教学工作台：http://127.0.0.1:8020')
print('教学数据目录：', root)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
'@ | & $tutorialPython -B -
```

打开 [http://127.0.0.1:8020](http://127.0.0.1:8020)，创建“教程练习”研究区和会话，依次输入“你好”和“我想研究一下 Agent”。观察消息、后台任务、来源和最终状态。用 `Ctrl+C` 关闭教学服务。8020 被占用时，在代码和访问地址中换一个空闲端口。

离线工作台适合看路由、任务与界面；论文理解、真实搜索和完整 Auto Research 需要真实配置。现有 `start-researchagent.ps1` 用于日常部署，它固定检查 8000，不用它启动这个隔离示例。

## 源码走读

按顺序打开：

1. [main.py](../../main.py)：`build_search()`、`main()` 如何组装依赖。
2. [models.py](../../research_agent/models.py)：`OfflineModel.complete()` 与 `model_from_env()`。
3. [fixtures/search_results.json](../../fixtures/search_results.json)：回答的固定来源是什么。
4. [trace.py](../../research_agent/trace.py)：事件如何按行写入。

搜索选择顺序是显式离线 fixture → `SEARCH_URL` → Tavily key → fixture。Web 真实研究路径还会检查配置缺失并报错，不能把 CLI 的默认降级行为当作 Web 真实模式保证。

## 面试表达与自测

“我保留了不依赖外部 API 的确定性闭环，用来检查工具调用、引用和失败退出。真实研究另用在线评测衡量，二者分开统计。每次执行都有 Trace，因此可以证明工具是否真的发生过。”

自测：关闭 API 后演示成功能证明什么？`--fail-search` 返回不足为什么可能仍是正常退出？为什么教学 Web 示例要显式给出 `db_path`？
