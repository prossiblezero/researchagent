# GitHub 发布范围

公开副本保留项目源码、Web 页面、教程、评测集、回归测试和核心评测代码。批量评测结果与本地运行状态不进入 GitHub 仓库。

## 保留什么

- `research_agent/`、Web 页面、CLI / 服务入口、依赖、启动与 CI 配置。
- `docs/` 中的 Markdown 文档，包括 `docs/tutorial/` 教程和 [docs/optimization-history/](optimization-history/README.md) 优化迭代历程（阶段改动、结果摘要、失败、提交索引与原始报告指纹）。
- 完整 `datasets/`，包括任务、标签、语料、来源与许可证说明及 `manifest.json`。导出不改写数据集，保留原始 SHA-256；`.gitattributes` 禁止 Git 转换数据集的换行符。
- `tests/` 和 `fixtures/`。两组原本依赖报告的回归测试改用 `tests/fixtures/` 中的小型固定输入。
- `scripts/export_source.py` 的 `CORE_EVALS` 列出的核心脚本：离线验收、V1/V2、检索、Agent / 证据问答、Auto Research、A-MEM、并行研究及其评分、审计依赖。

## 排除什么

- `evals/reports/`、`evals/archives/`、顶层评测结果 JSON、历史补丁与一次性报告/重放脚本。
- 压缩归档、日志、Trace、数据库、实验工作区、虚拟环境和真实 `.env`。
- 本地 `.git/` 历史与开发交接 `HANDOFF.md`。

文档中的历史数字保留原来的实验口径，但报告路径、源码快照和未选入的旧脚本属于本地研究档案，公开副本不提供这些文件。它们不代表在公开副本上重新测得的结果。重新运行评测会生成自己的结果；CI 也会生成少量验收产物作为 Actions artifact，但不提交进源码仓库。

外部评测数据的使用条件见 [DATA_LICENSE.md](../datasets/DATA_LICENSE.md)，不要把第三方原文视为项目自有授权。LongMemEval 任务文件约 52 MB，是保留的评测输入，因此精简后的源码仓库仍包含这个较大文件。

## 第一次发布

原开发仓库已经提交过大量结果，**直接 push 原仓库的 `main` 会上传旧历史**；`.gitignore` 不能从历史中移除它们。

在原仓库根目录运行：

```powershell
python scripts/export_source.py
```

这会新建 `data/github-publication/`，复制当前工作区的选定文件，不复制旧 Git 历史，也不覆盖已有目标目录。可传入另一个新目录用于再次导出。

进入导出目录后，安装依赖并执行与 CI 一致的离线检查：

```powershell
cd data/github-publication
python -m pip install -r requirements.txt
$env:OFFLINE_MODE = "1"
$env:PYTHONUTF8 = "1"
python evals/run_all.py
python evals/run_v1_eval.py --output evals/reports/v1-ci.json
python evals/run_v2_eval.py --output evals/reports/v2-ci.json
```

在 GitHub 的 `prossiblezero` 账号下创建空仓库 `researchagent`，按需要选择可见性，不预填 README。首次认证可用：

```powershell
git credential-manager github login --username prossiblezero --browser --force
```

确认终端仍在**导出目录**，且目标是自己的空仓库，再执行以下首次发布命令。如果副本已初始化或提交，只执行尚未完成的步骤。

```powershell
git init -b main
git add .
git status --short
git commit -m "Initial source release with tutorials and evaluation inputs"
git remote add origin https://github.com/prossiblezero/researchagent.git
git push -u origin main
```

后续提交继续使用这个新仓库；不要把原开发仓库的旧分支 merge 进来。需要同步本地开发进展时，重新导出到新目录，再审查差异并更新公开仓库的文件。
