"""JSONL trace writing and redaction at the untrusted-data boundary."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "api_key", "apikey", "access_token", "accesstoken", "refresh_token", "refreshtoken", "clientsecret",
    "password", "secret", "authorization", "token", "key",
}
SENSITIVE_SUFFIXES = ("token", "secret", "password", "authorization")
_CREDENTIAL_KEY = (
    r"(?i:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|secret|authorization|token)"
    r"|[A-Za-z][A-Za-z0-9]*(?:Key|Token|Secret|Password|Authorization)"
    r"|[A-Z][A-Z0-9]*(?:TOKEN|SECRET|PASSWORD|AUTHORIZATION)"
    r"|(?<=[?&\"'_-])(?i:key)"
)
_KEY_CLOSE = r"\\?[\"']?"
_SEPARATOR = r"\s*[:=]\s*"
_ASSIGNMENT_START = r"(?<![A-Za-z0-9])(?:" + _CREDENTIAL_KEY + r")" + _KEY_CLOSE + _SEPARATOR
_ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?P<key>" + _CREDENTIAL_KEY + r")"
    r"(?P<key_close>" + _KEY_CLOSE + r")(?P<separator>" + _SEPARATOR + r")"
    r"(?P<value>\[REDACTED\](?:(?!" + _ASSIGNMENT_START + r")[^&\s])*"
    r"|\"(?:\\.|[^\"\\\r\n])*(?:\"|(?=\r?\n|\Z))"
    r"|'(?:\\.|[^'\\\r\n])*(?:'|(?=\r?\n|\Z))"
    r"|\\\"(?:(?:\\){3}\"|\\(?!\")|[^\\\r\n])*(?:\\\"|(?=\r?\n|\Z))"
    r"|\\'(?:(?:\\){3}'|\\(?!')|[^\\\r\n])*(?:\\'|(?=\r?\n|\Z))"
    r"|[^&\s]+)"
)
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_JSON_COLON = re.compile(r"\s*:\s*")
_USERINFO = re.compile(r"(?i:(https?://)([^\s/@:]+):([^\s/@]+)@)")
_AUTHORIZATION = re.compile(
    r"(?i:(authorization\s*:\s*)(?:[A-Za-z][A-Za-z0-9_-]*\s+)?(?:\[REDACTED\]|[^\s\"',}\]]+))"
)
_JSON_CANDIDATE = re.compile(
    _USERINFO.pattern + r"|(?i:authorization\s*:\s*(?=[^\s\"'\\])(?:[A-Za-z][A-Za-z0-9_-]*\s+)?[^\s]+)"
    + "|" + _ASSIGNMENT.pattern + r'|(?P<json_start>[{\["])'
)


def is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")
    separated = ("_key", *(f"_{suffix}" for suffix in SENSITIVE_SUFFIXES))
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith(separated)
        or value.isupper() and normalized.endswith(SENSITIVE_SUFFIXES)
    )


def _redact_assignment(match: re.Match[str]) -> str:
    value = match["value"]
    quote = next((item for item in (r'\"', r"\'", '"', "'") if value.startswith(item)), "")
    close = quote if quote and value.endswith(quote) else ""
    if value.startswith("[REDACTED]"):
        # Markers cannot end a credential; preserve only trailing JSON closing punctuation.
        tail = value.replace("[REDACTED]", "")
        close = tail[len(tail.rstrip("\\\"',}]")):]
    return f"{match['key']}{match['key_close']}{match['separator']}{quote}[REDACTED]{close}"


def shorten(value: Any, limit: int = 1200) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _redact_json(text: str, limit: int | None = 12000) -> str:
    decoder = json.JSONDecoder(parse_int=str, parse_float=str)
    decoder.decode(text)  # Only use string boundaries from a complete, valid JSON value.
    output: list[str] = []
    end = 0
    for match in _JSON_STRING.finditer(text):
        if match.start() < end:
            continue
        value = json.loads(match[0])
        colon = _JSON_COLON.match(text, match.end())
        cleaned = redact(value, limit=limit)
        if cleaned != value:
            output.extend((text[end:match.start()], json.dumps(cleaned)))
            end = match.end()
        if colon:
            if not is_sensitive_key(value):
                continue
            start = colon.end()
            _, stop = decoder.raw_decode(text, start)
            replacement = '"[REDACTED]"'
            output.extend((text[end:start], replacement))
            end = stop
    return "".join(output) + text[end:]


def _redact_fragments(text: str, limit: int | None = 12000) -> str:
    decoder = json.JSONDecoder(parse_int=str, parse_float=str)
    output: list[str] = []
    end = 0
    for match in _JSON_CANDIDATE.finditer(text):
        # Shield whole credentials, including the suffix consumed by the second assignment pass.
        if match.start() < end or match["json_start"] is None:
            continue
        try:
            decoded, stop = decoder.raw_decode(text, match.start())
            if match["json_start"] == '"' and (
                _JSON_COLON.match(text, stop) or not decoded.lstrip().startswith(("{", "[", '"'))
            ):
                continue  # Only split quoted JSON containers; leave prose/assignments intact.
            cleaned = _redact_json(text[match.start():stop], limit=limit)
        except (json.JSONDecodeError, RecursionError):
            continue
        output.extend((_redact_text(text[end:match.start()]), cleaned))
        end = stop
    return "".join(output) + _redact_text(text[end:])


def _redact_text(value: str) -> str:
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)basic\s+[A-Za-z0-9+/=]+", "Basic [REDACTED]", value)
    value = _USERINFO.sub(r"\1[REDACTED]@", value)
    value = _AUTHORIZATION.sub(r"\1[REDACTED]", value)
    value = _ASSIGNMENT.sub(_redact_assignment, value)
    return re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", value)


def redact(value: Any, *, limit: int | None = 12000) -> Any:
    """Redact credentials; limit=None preserves bounded execution receipts/checkpoints."""
    # Match credential fields, not ordinary telemetry such as prompt_tokens.
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            output[redact(key_text, limit=limit)] = "[REDACTED]" if is_sensitive_key(key_text) else redact(item, limit=limit)
        return output
    if isinstance(value, (list, tuple)):
        return [redact(item, limit=limit) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[", '"')):
            try:
                return _redact_json(value, limit=limit)[:limit]
            except (json.JSONDecodeError, RecursionError):
                pass
        if r'\"' in value:
            try:
                # Providers also embed JSON with its outer quotes escaped.
                decoded = json.loads('"' + value + '"', strict=False)
                cleaned = redact(decoded, limit=limit)
                if cleaned == decoded:
                    return value[:limit]
                encoded = json.dumps(cleaned, ensure_ascii=False)[1:-1].encode("utf-8", "backslashreplace").decode("utf-8")
                controls = {"n": "\n", "r": "\r", "t": "\t"}
                # Keep literal Markdown line breaks without unescaping literal backslashes.
                encoded = re.sub(r"(?<!\\)((?:\\\\)*)\\([nrt])",
                                 lambda m: m[1] + controls[m[2]] if controls[m[2]] in value else m[0], encoded)
                return encoded[:limit]
            except (json.JSONDecodeError, RecursionError):
                pass
        return _redact_fragments(value, limit=limit)[:limit]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return str(value)[:1200]
    except Exception:
        return "[UNSERIALIZABLE]"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TraceWriter:
    def __init__(self, path: Path, run_id: str, on_event=None, *, append=False):
        self.path = path
        self.run_id = run_id
        self.seq = 0
        self.on_event = on_event
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if append and self.path.exists():
            with self.path.open(encoding='utf-8') as stream:
                for line in stream:
                    record=json.loads(line)
                    if record.get('run_id')!=run_id:
                        raise ValueError('Trace run identity mismatch')
                    self.seq=max(self.seq,record.get('seq',0))
        self._file = self.path.open("a" if append else "w", encoding="utf-8")

    def emit(self, event: str, **payload: Any) -> None:
        self.seq += 1
        record = {"run_id": self.run_id, "seq": self.seq, "ts": now_iso(), "event": event, **redact(payload)}
        self._file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._file.flush()
        if self.on_event:
            self.on_event(record)

    def close(self) -> None:
        self._file.close()
