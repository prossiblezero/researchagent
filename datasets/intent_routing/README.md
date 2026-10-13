# ResearchAgent 七类意图评测 v1

这是一套**项目适配评测集**，不是官方 MASSIVE 60 类意图 benchmark 成绩，也不测完整研究任务是否成功。

- 252 题：开发集 42 题（每类 6 题）、测试集 210 题（原始每类 30 题；标注复核后 CHAT 34、AUTO_RESEARCH 26，其余各 30）。七类为 CHAT、RESEARCH、LOCAL_QA、DOWNLOAD、BRAINSTORM、EXPERIMENT、AUTO_RESEARCH。
- 32 题来自 Amazon MASSIVE v1.1 官方 test split；只选能映射到本项目的 `general_joke`、`qa_maths`、`news_query`，分别映射到 CHAT、CHAT、RESEARCH。开发集 8 题、测试集 24 题，中英文各半。
- 220 题为实施代理在查看现有产品定义后编写、预先标注的场景；测试前另由独立审查代理核对产品规则、为 6 条尚未运行的题补齐主题或既定配置（审查记录在 JSON 内）；不是独立人工盲标或真实用户随机抽样。自建开发/测试场景分开编写，但可能共享表达和任务模式。
- 多轮指代、已给文本与待读资料、历史回忆、禁止联网、禁止执行、状态询问、引用中的指令和完整自动研究授权都有覆盖。只有路由分类被调用，不执行这些请求。

## 公开数据的来源与选择

原始来源：[AmazonScience/MASSIVE](https://huggingface.co/datasets/AmazonScience/massive)，MASSIVE 作者与 Amazon.com Inc. or its affiliates，许可证 [CC BY 4.0](https://huggingface.co/datasets/AmazonScience/massive/blob/main/LICENSE)。保留原句，不做翻译或改写；改动仅为映射到本项目七类标签。每题保留原始 ID、语言、意图和官方 split。

原文件 URL、字节数和 SHA256 均写入 `v1.json`。选择规则在任何模型预测前确定：过滤上述三类，排除 5 个主题缺失/含义不明确/媒体播放的新闻 ID（逐项理由见 `selection.exclusions`），对 ID 按 `SHA256("intent-routing-v1:20261012:" + id)` 升序排列，每个映射后项目类取 16 个，前 4 个作开发集，其余作测试集；依次交替选中文、英文。两个语言版本共享同一来源 ID，不重复抽取，也不跨开发/测试集。

官方来源 test 标签被用来构造本项目的新开发/测试划分；不能把本项目开发集说成 MASSIVE 官方 validation split。其他 57 个原始类别和其他语言不在本次结论范围内。

## 运行与评分

使用项目 Python 环境，配置现有模型服务后运行：

```powershell
python evals/run_intent_routing_eval.py --split dev --out evals/reports/intent-routing-v1/dev
python evals/run_intent_routing_eval.py --split test --out evals/reports/intent-routing-v1/test
python -m unittest discover -s tests -p test_intent_routing_eval.py
```

调用真实 `route_intent`，沿用生产系统提示、上下文裁剪、结构校验和一次格式修复；评测只额外设置输出上限 4096 token 和总预算。输入仅含产品路由提示、公开/合成会话和合成研究区元数据；标签、标签理由、样本 ID、分组信息不进入模型请求。每份结果记录数据/源码哈希、模型名、实际请求、原始回复、失败、时间和服务商用量。已有输出目录不可覆盖；不对失败题重跑挑最好结果。

开发集最多 420 秒、80 次物理请求、30 万已报告 token；测试集最多 1200 秒、280 次物理请求、150 万已报告 token。两组均最多 4 个并发分类、每题 60 秒。请求前用输入 UTF-8 字节数加输出上限和协议余量预留预算；并发中未返回的请求占用预算，未知用量按预留值计账。服务商用量另报覆盖率，不把未知当零。以上是调用记账限制，不是货币费用保证。

主指标为七类 exact accuracy 和 macro-F1；调用失败、非法结构、预算截止及缺失结果均留在完整分母中。另报逐类 precision/recall/F1、混淆矩阵、公开/自建分层、多轮和约束场景分层、AUTO_RESEARCH 对非自动研究题的误触发数、时延与 token。标签能确定性对比，因此不用 LLM-as-a-Judge 为分类打分。

随附的 OfflineRouter 只作为项目已有的关键词演示基线，它没有 AUTO_RESEARCH 分类能力；不可将其称为成熟竞品或“先进三路融合”基线。按类别配额构造的测试分布不等于生产流量。Wilson 区间假设题目独立，而自建题可能相关；只能作为描述性统计。若查看测试结果后修改路由，该测试集即转为回归集，后续提升应使用新的预先标注样本验证。

## 2026-10-12 首次实测与标签复核

正式测试调用真实生产路由及 `gpt-5.6-luna`，所有 210 题均保留首次结果；4 次服务超时计错。原始冻结标签下为 **193/210，91.90% accuracy，93.09% macro-F1**。正式测试启动后，由另一审查代理在不读取预测的条件下复核全部 AUTO_RESEARCH、EXPERIMENT 及公开新闻题（含开发集，共 88 题），发现 4 题虽授权自动研究，却没有研究主题且无历史可解析，应按既有产品规则先走 CHAT 澄清。

结果归档后纠正这 4 个 gold 标签，同一批首次回答为 **197/210，93.81% accuracy，94.98% macro-F1**；非自动研究题误触发 AUTO_RESEARCH 为 **0/184**。没有删题、改输入、改路由或重新调用模型，分数变化不代表系统性能提升。另有 1 道公开新闻题含义有歧义，未确认标错，保持原标签及失败计分。

原标签、理由和完整复核记录保存在 `v1.json` 的 `label_audit` 中；结合该字段可以重建原始冻结面板。两套指标、210 题预测及失败、请求/回复哈希与原始协议见[可复算结果](../../docs/research/intent-routing-results-20261012.json)，详细解释见[评测与裁判报告](../../docs/research/intent-routing-and-judge-20261012.md)。运行下面的离线检查可重建原面板哈希并复算两套分数，不调用模型：

```powershell
python -m unittest discover -s tests -p test_intent_routing_eval.py
```
