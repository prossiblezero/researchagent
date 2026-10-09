# 11 · FastAPI 服务层迁移

时间：2026-10-09。迁移仅涉及 HTTP/API 层，继续复用当前版本全部研究能力。

## 问题与实现

原标准库 HTTP Handler 集中手写路由、解析与错误响应，接口缺少自动生成的契约。改用 FastAPI / Uvicorn，路由按工作台与研究成果分组；常用写请求使用 Pydantic，提供 `/docs` 和含 74 个路径的 `/openapi.json`。

SQLite、Workbench、检索、Memory、上下文压缩、报告版本、双路研究、Codex 委派与有限实验继续使用原业务层。同步调用进入线程池，SSE 采用异步传输和持久游标。研究区共享资料与设置，会话独立，仅显式分支继承历史。

应用 lifespan 负责数据库独占和 worker 启停；活跃线程未退出时保留锁。本机权限依据实际绑定地址，禁止依赖代理头获取本机工具能力。木屋文件与真实 `.env` 哈希保持。

## 验证与失败记录

独立源码导出上运行 616 项 Python 测试：609 通过，7 项原生受限执行测试按默认开关跳过；前端 33/33、V1 6/6、V2 5/5，其余离线验收与依赖检查通过。真实 `server.py` 在独立数据库上完成离线聊天、研究报告、来源/证据/轨迹、独立会话和历史分支，并经过两次进程启动验证已有结果保留。

初轮回归虽通过断言，但暴露 Windows 连接关闭告警；后续还有一次非法来源请求的 TCP reset。已修复 socket 关闭顺序、Windows Proactor 回收问题及错误请求体提前关闭问题。独立审查发现的 lifespan 重入、localhost 权限和 OpenAPI 请求体不一致问题已修复并补回归，最终审查无遗留阻断。初轮日志及验收脚本的首次路径错误均保留。

本地完整记录位于 `data/fastapi-migration-20261009/report.md` / `report.json`，包括逐文件哈希、日志、入口验收 SQLite、报告、来源及轨迹。测试客户端仍提示 Starlette 的 httpx 适配器将废弃；当前版本可用，生产入口无该提示。

这次衡量 API 兼容和生命周期，不是新的模型效果评测，不改写既有简历指标。当前简历技术栈增加 FastAPI。

## 版本与使用

迁移前本地检查点 `9c38cdb4791d5e0d3eac8ed75e77a29b082faece`；公开检查点 `c630d7d8601dc8b15d8424b4b3c8c0dcbe5ae973` 和标签 [pre-fastapi-20261009](https://github.com/prossiblezero/researchagent/tree/pre-fastapi-20261009) 已上传，main/tag 两轮 Windows、Ubuntu CI 均通过。

迁移实现已保存为本地提交 `31eb0cda4b88a5bacfb687fb66fe1abc8f24d8d6`。本次公开同步包含迁移源码、测试、技术文档、优化历程与加入 FastAPI 的简历；迁移前标签继续保留作为回退点。公开提交身份以本文件的 Git 历史为准。启动方式仍是 `python server.py`，详细合同与单进程部署约束见 [FastAPI 服务层](../fastapi-service.md)。
