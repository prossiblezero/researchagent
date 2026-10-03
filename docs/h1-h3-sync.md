# H1–H3 跨仓库同步记录

日期：2026-09-16。来源 `D:\project\newagent`，目标 `D:\project\researchagent`。

## 结论与边界

H3 v7 复审通过，H1–H3 已按文件同步，目标仓库的回归和离线 CLI/API 联调通过。
V1 尚未开始；`newagent` 继续保持 `PAUSED_FOR_RESEARCHAGENT`，不开始 H4 或 Agent-RL。
本次没有修改 Harness 业务逻辑，也没有新增依赖。

## 版本与同步范围

- 源 HEAD：`c8f66a6eb3e6f5e3138a0dbd04b46e46435e1b6a`。交付来自未提交工作树，不能用 HEAD 代替文件哈希。
- H3 源与目标代码合并 SHA-256：`00c56bcd98d7fa935923f41f24d5ffa5051562c2688fa97179cd51b61813935f`。
- H3 评测文件合并 SHA-256：`cc2cc2ee6f605415e789cebd691cd3d3a04af9523b3b9cb140a8a07faeda880c`。
- 59 个交付文件中，57 个代码、评测、测试及共享教学文件与源一致；README 和 HANDOFF 按目标仓库的工作台定位合并。
- `h1-h3-delivery-manifest.sha256` 是源交付清单；目标实际文件以 `h1-h3-synced-files.sha256` 为准。目标无 Git 提交，本次用逐文件哈希定位，不自动提交。
- 保留目标 `.env`、`.git`、运行数据、历史报告和工作台路线；未复制缓存、临时开发目录、个人数据库或 Trace。

## 本次复核证据

在 `evidence-agent` Python 3.12.14 中执行，未使用 base，也未调用收费 API：

| 检查 | 结果 |
|---|---|
| 源交付清单核验 | 59 个文件，零缺失、零哈希不匹配 |
| 仅包含交付清单的空目录运行 `python -B evals/run_all.py` | 88 项单元测试通过；Stage 1 4/4；离线 21/21 次；Stage 3–7 PASS |
| 反向应用 `h3-p2-repair-v7.patch` 后执行同版 H3 runner | 6/10 行、92/118 断言，与保存的旧基线代码哈希及全部 metrics 一致 |
| 反向应用 `h3-p2-closeout-v7.patch` 后执行同版 H3 runner | 9/10 行、115/118 断言，与保存的收尾前代码哈希及全部 metrics 一致 |
| 当前 H3 runner | 10/10 行、118/118 断言、25/25 证据质量守卫，严格 gate PASS |
| 脱敏专项复核 | 原始 JSON、代码块、说明文字、属性名的旧反例均关闭；独立安全复核无新增阻塞 |
| 目标 `python -B evals/run_all.py` | 88 项单元测试、Stage 1 4/4、离线 21/21、Stage 3–7 PASS |
| 目标 CLI 与真实本地 HTTP 链路 | 聊天不显示空 Sources；研究有来源；页面、健康检查、研究提交、历史、详情、Trace 读取、SQLite 重开通过 |

API 联调使用离线模型、回环端口和独立临时数据库/Trace；未写用户运行数据库。既有测试覆盖旧 SQLite evidence 表的增列兼容。

## 指标解释与后续

这些分数证明固定输入下的 Harness 行为和回归，不是开放域研究成功率。
当前 H3 总输入为 27,622 bytes / 9,208 估算 token，比旧 P2 的 24,393 / 8,132 更高；本次脱敏收尾前后成本相同，不宣称压缩收益。
真实 dev C 的历史 150 次均因网络环境失败；成功、同版本的真实 A–D 质量对照仍待完成。

后续在 researchagent 单独启动 V1。V1 有实际代码和开发记录后，再由用户确认是否恢复 newagent 的 H4；本次同步不满足“V1 已开始”。

## 同步后的 V1 开发

以上状态和哈希是 Harness 同步时点的历史快照。2026-09-16 后续已在 researchagent 实际实现 V1，详见 [V1 验收](v1-validation.md)。loop.py 与 trace.py 为工作台增加了可选事件回调，context.py 补充思考模式兼容字段，loop.py 补充预算终止提示，因此当前源码不再逐字节等同此历史交付清单；原始快照仍可用于追踪来源。本次未修改 newagent 或恢复 H4。
