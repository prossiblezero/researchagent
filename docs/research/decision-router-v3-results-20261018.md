E2 准确率 158/160（98.75%），原 LLM（A）为 156/160（97.50%）；AUTO_RESEARCH 误触发分别为 0 和 0 次。
4B 免训练开发集最优为逆序顺序，macro-F1 67.96%；0.6B LoRA 为 94.03%，因此4B只作对照，未加入级联。
默认仍为原 LLM 路由；是否切换由用户决定。统一确认减少本地固定意图造成的风险，但不能保证自动研究永不误触发。

# 决策路由后续实验

## 主结果

|组|准确率|macro-F1|常规105题|边界35题|口语20题|AUTO误触发|非CHAT→CHAT|失败|ECE|回退率|AUTO确认次数|
|---|---|---|---|---|---|---|---|---|---|---|---|
|A|156/160（97.50%）|97.76%|101/105（96.19%）|35/35（100.00%）|20/20（100.00%）|0/134|3|1|—|—|0|
|D|134/160（83.75%）|85.56%|101/105（96.19%）|18/35（51.43%）|15/20（75.00%）|15/134|1|0|0.075|—|0|
|E2|158/160（98.75%）|98.57%|104/105（99.05%）|35/35（100.00%）|19/20（95.00%）|0/134|0|0|—|37.50%|40|
|C1-4B|103/160（64.38%）|61.50%|73/105（69.52%）|18/35（51.43%）|12/20（60.00%）|33/134|4|0|0.177|—|0|

D 与 C1-4B 仅做本地意图判定；E2 为0.6B LoRA加完整LLM确认。ECE使用开发集冻结温度，失败始终留在准确率分母；ECE只覆盖有概率输出的题。

## 时延与成本

|组|加载冷启动/秒|首个本地预热/秒|首个远程预热路由/秒|热态意图p50/p95/秒|热态完整路由p50/p95/秒|峰值显存/GiB|远程请求（含预热）|已报告token（含预热）|
|---|---|---|---|---|---|---|---|---|
|A|—|—|1.958|6.393/27.002|6.393/27.002|—|168|不完整（已记录488,182）|
|D|3.418|0.605|—|0.204/0.237|—/—|1.693|0|0|
|E2|3.109|0.442|5.017|0.412/18.400|3.994/19.066|1.702|98|218,773|
|C1-4B|13.207|0.913|—|0.117/0.132|—/—|8.329|0|0|

硬件为 RTX 4080 SUPER 16GB，本地模型使用bf16、关闭thinking且不量化。冷启动是本进程模型加载（含身份校验）；首个预热样本只作单点观测，不计算冷态分位数。热态在5次预热后计时，级联还包含排队、字段补全或完整回退；本地分类组不生成完整路由，故该列留空。每个远程组预算900秒、200次请求、100万已报告token。

## 改动一：统一AUTO确认与离线复算

cascade中本地top-1为AUTO_RESEARCH时直接调用完整route_intent，允许改判CHAT，记录auto_research_confirm；不再使用tau_auto或固定AUTO字段补全。其他意图及decision路径保持原有行为。

- test_v2：7题曾过门槛进入AUTO字段补全，最终直接接受0题；新规则新增完整确认0次、去掉字段补全7次，忽略重试的远程调用净增-7次。
- regression_v1：1题曾过门槛进入AUTO字段补全，最终直接接受0题；新规则新增完整确认0次、去掉字段补全1次，忽略重试的远程调用净增-1次。

旧记录中上述字段补全全部因预算字段校验失败而回退；v1那次误触发最终来自完整LLM路由，设计稿中的路径推断已更正。以上仅复算受影响题数与调用数，不推导新的准确率或token成本。

## 无主题授权与边界错题

- A：无主题授权15题的最终结果为CHAT 15题。
  边界错题或AUTO误触发：无。
- D：无主题授权15题的最终结果为AUTO_RESEARCH 15题。
  边界错题或AUTO误触发：v3-auto-boundary-chat-01（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-02（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-03（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-04（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-05（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-06（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-07（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-08（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-09（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-10（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-11（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-12（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-13（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-14（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-15（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-16（CHAT→LOCAL_QA）；v3-auto-boundary-chat-17（CHAT→LOCAL_QA）。
- E2：无主题授权15题的最终结果为CHAT 15题。
  边界错题或AUTO误触发：无。
- C1-4B：无主题授权15题的最终结果为AUTO_RESEARCH 15题。
  边界错题或AUTO误触发：v3-regular-brainstorm-09（BRAINSTORM→AUTO_RESEARCH）；v3-regular-brainstorm-15（BRAINSTORM→AUTO_RESEARCH）；v3-regular-experiment-01（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-02（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-03（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-04（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-05（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-07（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-08（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-09（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-11（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-12（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-14（EXPERIMENT→AUTO_RESEARCH）；v3-regular-experiment-15（EXPERIMENT→AUTO_RESEARCH）；v3-auto-boundary-chat-01（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-02（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-03（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-04（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-05（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-06（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-07（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-08（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-09（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-10（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-11（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-12（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-13（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-14（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-15（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-17（CHAT→AUTO_RESEARCH）；v3-auto-boundary-chat-18（CHAT→RESEARCH）；v3-colloquial-experiment-01（EXPERIMENT→AUTO_RESEARCH）；v3-colloquial-experiment-02（EXPERIMENT→AUTO_RESEARCH）；v3-colloquial-experiment-03（EXPERIMENT→AUTO_RESEARCH）。

## 配对比较与复现

E2−A准确率差为 +1.25 个百分点；E2单独正确 4 题，A单独正确 2 题。McNemar精确双侧检验 p=0.6875，未做bootstrap。

v3 SHA-256：`a28b686618556f9fb6cb8bc51e970c729ad7e0e35cf0d5da697a558dfea86062`；测试计划 SHA-256：`cdd49df3fb25653a5b788081736ae3525b73414efc961ab69782c4c929a7902f`。独立作者仅接触标签政策、产品规则和场景标签；另一代理在不看预测的条件下逐题复核。search_scope在每类内均衡；与v1/v2全集及v3内部完成精确、5-gram Jaccard≥0.6、BGE-M3余弦≥0.92去重，记录保存在数据和JSON。

4B固定版本为`Qwen/Qwen3-4B@1cfa9a7208912126459214e8b04321603b3df60c`，Apache-2.0。与0.6B的选项提示哈希一致；三个顺序的开发记录、温度及阈值选择、原始逐题结果与全部错误ID见同名JSON。原v2报告未修改。

## 局限

- 本集由独立盲态代理编写与复核，非人工标注；规模160题且场景仍有相关性。
- 测试分布为产品意图与边界场景，不代表真实流量，也不衡量后续研究任务成功率。
- 本地GPU与远程API硬件、并发和网络不同；时延与token仅代表这次部署观测，不等于同硬件加速或货币成本。
- 只用一个LoRA种子，未补多种子或KL消融；4B顺序和校准只由dev_v2决定。
- LLM确认仍可能错误授权；本集零误触发也不构成生产安全保证，默认保持llm。
- 每组只运行一次，失败计错；McNemar只描述本集配对差异，不能证明总体等效。
