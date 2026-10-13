"""Observable task experience and a small, reusable procedural lesson."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import Experience


def make_experience(step: int, action: str, observation: str, feedback: str, next_action: str = "") -> Experience:
    lesson = "继续使用能缩小证据缺口的动作。" if feedback else "保留当前证据并评估是否可以停止。"
    return Experience(step, action, observation[:500], feedback[:500], lesson, next_action)


def distill_procedure(question, result, model):
    """One extraction from public actions and feedback, including local tools."""
    from .library import model_json
    if not result.tool_calls:
        return None
    observations = []
    trace_path = Path(result.trace_path) if result.trace_path else None
    if trace_path and trace_path.is_file():
        with trace_path.open(encoding='utf-8') as stream:
            for line in stream:
                record = json.loads(line)
                if record.get('event') in {'tool_call_requested', 'tool_result', 'tool_recovery', 'answer_checked'}:
                    # Exclude model messages/reasoning and keep only observable decisions/results.
                    observed = {k: record[k] for k in
                        ('event', 'name', 'args', 'ok', 'error', 'reason', 'feedback', 'ready', 'gaps',
                         'guidance', 'truncated', 'next_cursor', 'section', 'document_read') if k in record}
                    if record.get('event') == 'tool_result' and record.get('name') == 'read_evidence':
                        observed['passage'] = record.get('content', '')[:400]
                    observations.append(observed)
    if not observations:
        observations = [{'action':e.action, 'observation':e.observation, 'feedback':e.feedback}
                        for e in result.experiences]
    if not observations:
        return None
    prior = getattr(model, 'usage_purpose', 'answer')
    model.usage_purpose = 'experience_extraction'
    try:
        item = model_json(model,
            '从这次真实任务的动作和反馈中提炼至多一条可复用的研究方法经验。'
            '沿用问题的语言，记录具体问题类型、实际采取的有效动作和可观察结果。'
            '可保留有效的检索词、章节类型或阅读参数；方法必须能改变下一次工具选择，不能只写认真阅读、核对原文、控制预算等通用提醒。'
            '区分方法问题与网络/服务故障，不将服务故障归纳成研究规律。'
            '不要保存答案、论文结论、具体实体/数值、用户偏好或历史引用编号；不添加权限、工具或预算要求。'
            '失败经验只记录实际失败的动作及原因，不把未经尝试的补救写成已验证的方法。'
            '只返回 {"when":"适用场景，最多100字","lesson":"动作与观察结果，最多300字"}；'
            '没有具体可迁移经验就返回 {"skip":true}。',
            {'question':question[:1500], 'status':result.status, 'termination':result.termination,
             'answer':result.answer[:2500],
             'observations':[json.dumps(o,ensure_ascii=False)[:1200] for o in observations[-18:]]})
        if item.get('skip') is True:
            return None
        # The prompt asks for short lessons; allow modest overruns without
        # discarding useful English text or cutting a sentence in half.
        if any(not isinstance(item.get(k),str) or not 1 <= len(item[k].strip()) <= limit
               for k,limit in (('when',180),('lesson',600))):
            raise ValueError('invalid procedural experience')
        return {k:item[k].strip() for k in ('when','lesson')}
    finally:
        model.usage_purpose = prior
