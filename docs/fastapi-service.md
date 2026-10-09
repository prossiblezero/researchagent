# FastAPI 服务层

HTTP 层使用 FastAPI 0.143.0 / Uvicorn 0.53.0。检索、记忆、上下文管理、会话、研究队列与 CodingTool 继续由原 Workbench 执行，SQLite 数据模型及木屋前端保持兼容。

## 启动与接口

安装 `requirements.txt` 后仍运行 `python server.py`，沿用 `HOST`、`PORT`、`DB_PATH`、`OFFLINE_MODE` 与原模型配置。已有 PowerShell 启动脚本和 Docker 入口不变。

- `/`：木屋工作台；`/health`：运行模式与本机能力。
- `/docs`：Swagger UI；其静态资源来自 jsDelivr，无法访问 CDN 时可直接查看 `/openapi.json`。
- `/openapi.json`：路径、请求体和校验错误的 OpenAPI 契约。
- 原 `/api/spaces`、会话、资料、研究成果、实验、策略与旧研究接口保持路径兼容。

`server.create_app()` 可供 ASGI 测试和集成。应用构造不打开数据库，lifespan 启动时获取数据库独占锁并启动队列；停止时等待线程退出后释放锁。不要对同一数据库运行多个 worker 或自动重载：第二个服务会在队列恢复前被拒绝。未退出的后台线程仍持有数据库锁，再次关闭可重试回收。

嵌入式 `make_server()` 保留现有测试与启动器的 `serve_forever / shutdown / server_close` 接口，底层统一使用 Uvicorn。Windows 使用 SelectorEventLoop，避开当前 Windows Python 运行时 Proactor 在连接重置后不能完整回收 transport 的问题；这适合当前少量本地连接，编码进程由原工作线程通过同步 subprocess 启动。

## 请求与持久任务

`research_agent/http_api.py` 负责工作台、会话、资料及研究任务；`http_records.py` 负责研究成果、策略和实验；`http_support.py` 保存请求模型及 SSE 辅助函数。普通同步业务由 FastAPI 线程池调用；资料上传保存与 SSE 数据库轮询也在线程池内执行。研究任务不放入 HTTP 请求生命周期，刷新页面或 SSE 断开不会取消任务。

SSE 保留 `Last-Event-ID` / `after` 游标及研究区、会话归属检查，每条连接最多持续 45 秒后由前端续接。草稿与最终核验状态继续区分。停止任务仍需调用原停止/取消接口。

研究区共享资料、Notes 和设置；研究区内可有多个独立会话，只有显式历史分支继承消息。

## 校验与本机能力

常用消息、会话、研究区和自动研究请求使用严格 Pydantic 模型；研究成果的复杂合同继续复用原领域校验。参数错误统一返回 HTTP 400 和 `{"error": "..."}`，避免回显请求中的私密值；OpenAPI 与该错误格式一致。无业务参数的写操作也显式声明 JSON 请求体，请发送 `{}`。

写入继续检查 Origin / Sec-Fetch-Site，JSON 请求体上限为 100000 字节，二进制资料上传上限为 25 MB。先有界接收请求再检查来源、解析和调用领域方法，确保 Windows 客户端能收到错误响应；不在来源核验前写入。文件导入、trace、评测文件及实验产物仍执行原路径边界校验。

Uvicorn 禁用代理头信任。本机 Windows 工具同时检查实际绑定地址、直接客户端地址和 Host。`HOST=localhost` 会使用解析后的回环绑定；公开监听不会因伪造 Host 或 X-Forwarded-For 获得本机执行能力。直接 `create_app()` 未指定绑定地址时默认不允许原生工具。本服务仍面向单机，不提供多用户认证。

## 验证

`tests/test_fastapi.py` 覆盖应用重入、worker 唯一性、数据库锁保留、请求合同、同源/体积限制、同步请求并发和 localhost 权限；原会话、资料、SSE、报告与实验测试继续验证业务兼容性。

```bash
python evals/run_all.py
python evals/run_v1_eval.py --output evals/reports/v1-fastapi.json
python evals/run_v2_eval.py --output evals/reports/v2-fastapi.json
node --test tests/test_workbench_frontend.cjs
```

Starlette 1.7 的 TestClient 会提示现有 httpx 适配器将废弃；当前依赖组合仍支持它，该提示仅属于测试客户端，不影响服务端运行。
