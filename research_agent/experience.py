"""Task-local experience records; no long-term memory in this stage."""

from __future__ import annotations

from .contracts import Experience


def make_experience(step: int, action: str, observation: str, feedback: str, next_action: str = "") -> Experience:
    lesson = "继续使用能缩小证据缺口的动作。" if feedback else "保留当前证据并评估是否可以停止。"
    return Experience(step, action, observation[:500], feedback[:500], lesson, next_action)
