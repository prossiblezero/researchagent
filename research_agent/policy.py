"""Small policy hooks kept outside the main loop."""

from __future__ import annotations

import re
from .contracts import AuditEvent, SYSTEM_PROMPT
from .search import canonical_url, url_rejection_reason
from .trace import redact, shorten


def before_tool(name: str, query: str = "", url: str = "", allowed_urls: set[str] | None = None) -> AuditEvent:
    if name not in {"search", "read"}:
        return AuditEvent("before_tool", "deny", "unknown_tool", shorten(redact(name), 200))
    if name == "search" and (not query.strip() or len(query) > 300):
        return AuditEvent("before_tool", "recover", "invalid_search_query", shorten(redact(query), 300))
    if name == "search" and any(ord(c) < 32 and c not in "\t\n" for c in query):
        return AuditEvent("before_tool", "recover", "control_character_in_query", shorten(redact(query), 300))
    if name == "read":
        normalized = canonical_url(url)
        if normalized is None:
            reason = url_rejection_reason(url)
            decision = "deny" if reason in {"credential_url", "network_target_denied"} else "recover"
            target = "[REDACTED_CREDENTIAL_URL]" if reason == "credential_url" else shorten(redact(url), 500)
            return AuditEvent("before_tool", decision, reason, target)
        allowed = {item for value in (allowed_urls or set()) if (item := canonical_url(value))}
        if allowed_urls is not None and normalized not in allowed:
            return AuditEvent("before_tool", "deny", "read_not_authorized", shorten(redact(normalized), 500))
        return AuditEvent("before_tool", "allow", target=normalized)
    return AuditEvent("before_tool", "allow", target=shorten(redact(query), 300))


def before_finalize(answer: str) -> AuditEvent:
    # Do not block ordinary quoted source text containing words like "secret".
    # Review only patterns that look like an actual credential/prompt dump.
    sensitive = (
        r"(?i)(?:password|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}['\"]?",
        r"(?i)api[_-]?key\s*[:=]\s*['\"]?(?:sk-|tvly-)[A-Za-z0-9_\-]{12,}['\"]?",
        r"(?i)bearer\s+[A-Za-z0-9._-]{12,}",
        r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    )
    if SYSTEM_PROMPT.strip()[:120] in answer or any(re.search(pattern, answer) for pattern in sensitive):
        return AuditEvent("before_finalize", "review", "possible_sensitive_output")
    return AuditEvent("before_finalize", "allow")
