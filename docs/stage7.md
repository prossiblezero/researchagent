# Stage 7：服务化与部署

## 问题

CLI 适合调试，但其他程序需要稳定 HTTP 接口；服务也要能离线演示。

## 解决方案

标准库 `http.server` 提供浏览器 UI、`GET /health`、`POST /research`，并用 Python 内置 SQLite 持久化历史运行。设置 `OFFLINE_MODE=1` 会强制 fixture + OfflineModel，不消耗 API 额度。

## 试一下

```powershell
conda activate evidence-agent
$env:OFFLINE_MODE='1'; python server.py
```

另开 PowerShell：

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod -Method Post http://localhost:8000/research -ContentType 'application/json' -Body '{"question":"请查明 learn-claude-code 是什么"}'
```

也可以直接打开 <http://localhost:8000>：输入问题后查看答案、来源、Claims 和历史任务。接口还提供 `GET /api/runs`、`GET /api/runs/{run_id}`、`GET /api/traces/{run_id}`。

返回 `answer`、`sources`、`evidence`、`claims`、`experiences`、`audit_events` 和 `trace_path`。缺少 `question` 或超过 100000 字节返回 HTTP 400。

真实部署：在 `.env` 配置模型和 `TAVILY_API_KEY` 后执行：

```powershell
docker build -t evidence-agent .
docker run --rm -p 8000:8000 --env-file .env evidence-agent
```

SQLite 文件默认位于 `data/evidence_agent.db`，可通过 `DB_PATH` 调整。Docker 只负责打包，不改变 Stage 1–6 的 loop、Trace 和评测；生产环境应把 `data/` 挂载为 volume。
