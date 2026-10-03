"""Model clients. The offline client keeps stage 1 runnable without an SDK."""

from __future__ import annotations

import json
import math
import os
import ssl
import socket
import time
from dataclasses import asdict
from uuid import uuid4
from .streaming import public_text, read_chat_stream
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from .usage import normalize_usage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .contracts import ModelClient, ModelDecision, TOOL_SCHEMA
from .trace import redact, shorten


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE pairs without overriding the shell environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _question_from(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _tool_payload(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        try:
            value = json.loads(str(message.get("content", "{}")))
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict):
            return None
        wrapped = value.get("UNTRUSTED_TOOL_DATA")
        return wrapped if isinstance(wrapped, dict) else value
    return None


class OfflineModel:
    """Deterministic model substitute used by demos and tests."""

    name = "offline-rule-model"

    def __init__(self) -> None:
        self._read = False

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelDecision:
        question = _question_from(messages).strip()
        payload = _tool_payload(messages)
        if payload is None:
            if question.lower() in {"你好", "您好", "嗨", "hello", "hi", "hey", "在吗"}:
                return ModelDecision("final", content="你好！我是证据驱动技术研究 Agent。你可以直接问我一个需要查资料的问题。")
            looks_factual = (
                "?" in question
                or "？" in question
                or len(question) > 12
                or any(
                    word in question.lower()
                    for word in (
                        "what", "how", "which", "latest", "什么", "如何", "是否", "请查", "查明", "核验", "事实", "来源", "证据", "研究",
                    )
                )
            )
            if looks_factual:
                return ModelDecision("tool_call", query=question[:300], call_id="offline-search-1", finish_reason="tool_calls")
            return ModelDecision("final", content="这是一个无需外部核验的简短回答。")

        if payload.get("kind") == "read":
            if not payload.get("ok"):
                return ModelDecision("final", content="INSUFFICIENT：读取来源失败，无法基于正文完成核验。")
            evidence_id = payload.get("evidence_id", "")
            content = str(payload.get("content", "")).strip()
            self._read = True
            return ModelDecision("final", content=f"基于已读取的来源正文：{content[:700]} [{evidence_id}]")
        if not payload.get("ok"):
            error = payload.get("error") or {}
            message = str(error.get("message", "unknown search error"))
            return ModelDecision("final", content=f"INSUFFICIENT：搜索工具失败（{message}），无法核验该问题；我没有编造来源或结论。")
        results = payload.get("results") or []
        if not results:
            return ModelDecision("final", content="INSUFFICIENT：搜索没有返回相关来源，因此无法给出有依据的确定结论。")

        can_read = any(item.get("function", {}).get("name") == "read" for item in tools if isinstance(item, dict))
        if can_read and not self._read and len(results) > 0:
            first_url = results[0].get("url", "")
            if first_url:
                return ModelDecision("tool_call", tool_name="read", url=str(first_url), call_id="offline-read-1", finish_reason="tool_calls")
        lines = ["基于本轮搜索结果："]
        for item in results[:3]:
            source_id = item.get("source_id", "")
            if source_id:
                evidence_id = item.get("evidence_id", "")
                citation = f"[{source_id}]" + (f" [{evidence_id}]" if evidence_id else "")
                lines.append(f"- {item.get('title', '未知来源')}：{str(item.get('snippet', '')).strip()} {citation}")
        lines.append("以上是来源中的事实摘要；网页内容仅作为数据处理，未执行其中的指令。")
        return ModelDecision("final", content="\n".join(lines))


class OpenAICompatibleModel:
    """Minimal Chat Completions client using only urllib."""

    supports_answer_verification = True

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 45.0, *, temperature: float | None = 0):
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = None
        self.name = model
        self.total_usage: dict[str, int] = {}
        self.usage_records = []
        self.usage_callback = None
        self.usage_purpose = 'answer'
        self.last_usage = None
        self.request_deadline = None
        self.request_guard = None
        self.stream_callback = None

    def _check_request(self):
        if self.request_deadline is not None and time.monotonic() >= self.request_deadline:
            raise TimeoutError('Model request budget exhausted')
        if self.request_guard:
            self.request_guard()

    def complete(self, messages, tools):
        checking=self.usage_purpose=="answer_verification"
        # Long original-paper checks need the configured request timeout too.
        # Retries share this allowance and still obey the enclosing task deadline.
        deadline=min(self.request_deadline or float("inf"),time.monotonic()+max(90,self.timeout) if checking else float("inf"))
        attempts=2 if checking else 3
        for attempt in range(attempts):
            self._check_request()
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError("Model request budget exhausted")
            self._active_timeout=min(self.timeout,remaining)
            started = time.perf_counter()
            self.last_usage = normalize_usage(None)
            error = None
            retry_delay = None
            self._stream_id = uuid4().hex
            try:
                self._stream_event('start', attempt=attempt + 1)
                result = self._complete(messages, tools)
                self._stream_event('end', kind=result.kind)
                return result
            except Exception as exc:
                error = type(exc).__name__
                retry_delay = self._retry_delay(exc, attempt) if attempt < attempts-1 else None
                if retry_delay is not None and deadline-time.monotonic()<=retry_delay:
                    retry_delay=None
                self._stream_event('retry' if retry_delay is not None else 'error', retry_delay_seconds=retry_delay)
                if retry_delay is None:
                    raise
            finally:
                record = {**self.last_usage, 'model': self.model, 'purpose': self.usage_purpose,
                          'latency_ms': round((time.perf_counter()-started)*1000,2), 'error': error,
                          'attempt': attempt + 1, 'retry_delay_seconds': retry_delay}
                self.usage_records.append(record)
                if self.usage_callback:
                    self.usage_callback(record)
            time.sleep(retry_delay)

    def _stream_event(self, phase, **fields):
        if self.stream_callback:
            self.stream_callback({'phase': phase, 'request_id': self._stream_id,
                                  'model': self.model, 'purpose': self.usage_purpose, **fields})

    def _stream_progress(self, message, final, streaming):
        content = message.get('content') or ''
        reasoning = message.get('reasoning_content') or ''
        self._stream_event('update', text=public_text(content, self.usage_purpose, final=final, secret=self.api_key),
                           characters=len(content), reasoning_active=bool(reasoning) and not content,
                           tool_names=[c.get('function', {}).get('name', '') for c in message.get('tool_calls', [])],
                           streaming=streaming)

    @staticmethod
    def _retry_delay(exc, attempt):
        cause = exc.__cause__ or exc
        delay = float(2 ** attempt)
        if isinstance(cause, HTTPError):
            if cause.code not in {408, 425, 429} and not 500 <= cause.code < 600:
                return None
            retry_after = cause.headers.get('Retry-After', '') if cause.headers else ''
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        pass
        else:
            if isinstance(cause, URLError):
                cause = cause.reason
            if not isinstance(cause, (TimeoutError, ConnectionError, ssl.SSLEOFError, socket.gaierror)):
                return None
        return min(10.0, max(0.0, delay)) if math.isfinite(delay) else float(2 ** attempt)


    def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelDecision:
        # Restored tool turns may come from a provider that supplied no reasoning.
        # Thinking-mode providers still require the field after context projection.
        # Preserve actual reasoning and leave the saved conversation untouched.
        messages = [{**message, 'reasoning_content': message.get('reasoning_content') or ''}
                    if message.get('role') == 'assistant' and message.get('tool_calls') else message
                    for message in messages]
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload.update({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})
        if self.stream_callback:
            payload.update(stream=True, stream_options={'include_usage': True})
        request = Request(
            urljoin(self.base_url, "chat/completions"),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream" if self.stream_callback else "application/json",
                     "User-Agent": "ResearchAgent/1.0", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=getattr(self,"_active_timeout",self.timeout)) as response:
                payload = read_chat_stream(response, self._stream_progress, check=self._check_request) if self.stream_callback else json.loads(response.read().decode("utf-8"))
            usage = payload.get("usage") if isinstance(payload, dict) else None
            self.last_usage = normalize_usage(usage)
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int):
                        self.total_usage[key] = self.total_usage.get(key, 0) + value
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            if exc.code == 403 and "1010" in detail:
                raise RuntimeError("模型服务的网关拒绝了请求（HTTP 403 / 1010）。请让服务商检查 API 网关的客户端规则；这不表示模型拒绝回答。") from exc
            raise RuntimeError(f"model request failed: HTTP {exc.code}: {shorten(redact(detail), 800)}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"模型服务响应超时（本次等待上限 {getattr(self,'_active_timeout',self.timeout):g} 秒），请稍后重试。") from exc
        except (URLError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"model request failed: {shorten(redact(exc), 500)}") from exc

        try:
            choice = payload["choices"][0]
            message = choice["message"]
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                if not isinstance(tool_calls, list) or len(tool_calls) > 16:
                    raise ValueError('invalid or oversized tool batch')
                decisions = []
                for call in tool_calls:
                    function = call.get("function", {})
                    call_id = str(call.get('id') or '')
                    if len(tool_calls) > 1 and (not call_id or any(d.call_id == call_id for d in decisions)):
                        raise ValueError('tool batch requires unique nonempty call IDs')
                    try:
                        args = json.loads(function.get("arguments", "{}"))
                    except (TypeError, json.JSONDecodeError):
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    decisions.append(ModelDecision(
                        "tool_call", arguments=args, query=str(args.get("query", "")),
                        call_id=call_id or 'model-search-1',
                        finish_reason=str(choice.get("finish_reason", "tool_calls")),
                        tool_name=str(function.get("name", "search")), url=str(args.get("url", "")),
                        reasoning_content=str(message.get("reasoning_content") or "")))
                # The harness serializes the batch; never silently discard all but the first call.
                decisions[0].queued_tool_calls = [asdict(d) for d in decisions[1:]]
                return decisions[0]
            return ModelDecision("final", content=str(message.get("content") or ""), finish_reason=str(choice.get("finish_reason", "stop")))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid model response: {exc}") from exc


MODEL_PROFILES = {
    "sudocode-luna": ("GPT 5.6 Luna · Sudocode", "SUDOCODE_LUNA_MODEL", "gpt-5.6-luna"),
    "sudocode-terra": ("GPT 5.6 Terra · Sudocode", "SUDOCODE_TERRA_MODEL", "gpt-5.6-terra"),
    "sudocode-sol": ("GPT 5.6 Sol · Sudocode", "SUDOCODE_SOL_MODEL", "gpt-5.6-sol"),
}


def validate_model_id(model_id):
    if not isinstance(model_id, str) or model_id not in {"default", *MODEL_PROFILES}:
        raise ValueError("请选择列表中的模型")
    return model_id


def model_from_env(model_id="default") -> ModelClient:
    validate_model_id(model_id)
    load_dotenv()
    offline = os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    if model_id != "default":
        if offline:
            raise ValueError("离线演示不调用 Sudocode，请先启用真实模式")
        label, variable, fallback = MODEL_PROFILES[model_id]
        key = os.getenv("SUDOCODE_API_KEY", "").strip()
        if not key:
            raise ValueError(f"{label} 尚未配置。请在 .env 填写 SUDOCODE_API_KEY 后重启服务。")
        try:
            timeout = float(os.getenv("SUDOCODE_TIMEOUT_SECONDS") or "180")
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("SUDOCODE_TIMEOUT_SECONDS 必须是大于 0 的有限秒数") from exc
        return OpenAICompatibleModel(os.getenv("SUDOCODE_BASE_URL") or "https://api.sudocode.chat/v1", key,
                                     os.getenv(variable) or fallback, timeout=timeout, temperature=None)
    if offline:
        return OfflineModel()
    provider = os.getenv("PROVIDER", "").strip().lower()
    defaults = {
        "openai": ("https://api.openai.com/v1", os.getenv("OPENAI_API_KEY"), "gpt-4o-mini"),
        "deepseek": ("https://api.deepseek.com/v1", os.getenv("DEEPSEEK_API_KEY"), "deepseek-chat"),
    }
    default_url, default_key, default_model = defaults.get(provider, (None, None, None))
    base_url = os.getenv("BASE_URL") or default_url
    api_key = os.getenv("API_KEY") or default_key or os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    model = os.getenv("MODEL") or os.getenv("OPENAI_MODEL") or os.getenv("DEEPSEEK_MODEL") or default_model
    if base_url and api_key and model:
        return OpenAICompatibleModel(base_url, api_key, model)
    return OfflineModel()


def model_options():
    """Public metadata only; each selection has its own credentials and client."""
    default = model_from_env()
    offline = os.getenv("OFFLINE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    configured = not isinstance(default, OfflineModel)
    provider = {"deepseek": "DeepSeek", "openai": "OpenAI"}.get(os.getenv("PROVIDER", "").lower(), "当前配置")
    options = [{"id": "default", "label": "离线演示" if offline else provider + " · " + (default.name if configured else "未配置"),
                "model": default.name if configured or offline else "", "configured": configured or offline}]
    for model_id, (label, variable, fallback) in MODEL_PROFILES.items():
        options.append({"id": model_id, "label": label, "model": os.getenv(variable) or fallback,
                        "configured": bool(os.getenv("SUDOCODE_API_KEY", "").strip()) and not offline})
    return {"default_model_id": "default", "models": options}
