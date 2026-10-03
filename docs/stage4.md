# Stage 4：验证 Claim 与处理冲突

## 问题

`[E1]` 只说明引用存在，不说明证据真的支持这句话；不同来源还可能互相矛盾。

## 解决方案

`verify.py` 只接收 Claim 和 Evidence，输出 `SUPPORTED`、`REFUTED`、`INSUFFICIENT`、`CONFLICTING`。

## 工作原理

当前是可复现的 deterministic baseline：计算主张与证据的 token 重叠；显式否定词视为反驳；同时出现支持和反驳则为冲突。这不是语义模型，同义改写可能被判为不足。

```python
claim = Claim("C1", "python is fast", ["E1"])
verify_claims([claim], [Evidence("E1", "S1", "python is fast")])
# C1.status == "SUPPORTED"
```

## 试一下

```powershell
python evals/run_all.py
```

结果写入 `claims_verified` Trace，并随 `/research` 返回。

## 接下来的章节

Stage 5 把验证反馈转成下一步研究动作。
