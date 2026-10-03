"""Normalize search results and check the citations in a final answer."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .contracts import Evidence, Source
from .search import canonical_url, summarize_content
from .trace import now_iso, redact, shorten


MAX_EVIDENCE_CHARS = 200_000


def normalize_citation_format(answer: str) -> str:
    """Normalize explicit grouped IDs, preserving location labels and every ID for validation."""
    def replace(match):
        body=match.group(1)
        ids=re.findall(r'(?<![A-Za-z0-9])([SE]\d+)(?![A-Za-z0-9])',body)
        if not ids or re.search(r'[SE]\d+\s*[-–]\s*(?:[SE])?\d+',body):
            return match.group(0)  # Never guess ranges or turn malformed IDs into valid evidence.
        rest=re.sub(r'(?<![A-Za-z0-9])[SE]\d+(?![A-Za-z0-9])','',body).strip(' ,，;；、')
        return ' '.join('['+ref+']' for ref in ids)+(('（'+rest+'）') if rest else '')
    for left,right in (('[',']'),('【','】'),('［','］')):
        answer=re.sub(re.escape(left)+r'([SE]\d+(?:[\s,，;；、][^\]】］\n]{0,120})?)'+re.escape(right),replace,answer)
    return answer


class SourceCatalog:
    """The small bit of state that turns provider rows into local source IDs."""

    def __init__(self) -> None:
        self.items: list[Source] = []
        self._by_url: dict[str, Source] = {}

    def add(self, rows: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(rows, list):
            return normalized
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            url = canonical_url(str(raw.get("url", "")).strip())
            if url is None:
                continue
            source = self._by_url.get(url)
            if source is None:
                published = raw.get("published_at")
                source = Source(
                    source_id=f"S{len(self.items) + 1}",
                    title=shorten(str(raw.get("title", "")).strip() or "Untitled source", 500),
                    url=url,
                    snippet=shorten(str(raw.get("snippet") or raw.get("content") or "").strip(), 2000),
                    publisher=shorten(redact(raw.get("publisher", "")), 300),
                    published_at=None if published is None else shorten(redact(published), 100),
                    retrieved_at=now_iso(),
                )
                self._by_url[url] = source
                self.items.append(source)
            normalized.append({
                "source_id": source.source_id,
                "title": source.title,
                "url": source.url,
                "snippet": source.snippet,
                "publisher": source.publisher,
                "published_at": source.published_at,
                "retrieved_at": source.retrieved_at,
            })
        return normalized

    def source_id_for(self, url: str) -> str:
        normalized = canonical_url(url)
        source = self._by_url.get(normalized) if normalized else None
        return source.source_id if source else ""


class EvidenceCatalog:
    def __init__(self) -> None:
        self.items: list[Evidence] = []
        self._by_key: dict[tuple[str, str], Evidence] = {}

    def add(
        self,
        source_id: str,
        content: str,
        kind: str = "snippet",
        *,
        summary: str = "",
        title: str = "",
        content_hash: str = "",
        truncated: bool = False,
        retrieved_at: str = "",
        provenance: dict | None = None,
    ) -> Evidence:
        key = (source_id, kind)
        if key in self._by_key:
            return self._by_key[key]
        full_content = str(content).strip()
        content_was_cut = len(full_content) > MAX_EVIDENCE_CHARS
        evidence = Evidence(
            evidence_id=f"E{len(self.items) + 1}",
            source_id=source_id,
            content=full_content[:MAX_EVIDENCE_CHARS],
            kind=kind,
            retrieved_at=retrieved_at or now_iso(),
            summary=str(summary).strip() or summarize_content(full_content),
            title=shorten(str(title).strip(), 500),
            content_hash=content_hash or hashlib.sha256(full_content.encode("utf-8")).hexdigest(),
            truncated=bool(truncated or content_was_cut),
            provenance=dict(provenance or {}),
        )
        self.items.append(evidence)
        self._by_key[key] = evidence
        return evidence


def claims_from_answer(answer: str, evidence: list[Evidence]) -> list[Any]:
    """Create lightweight Claim records from cited answer lines."""
    from .contracts import Claim

    by_evidence = {item.evidence_id for item in evidence}
    claims: list[Claim] = []
    for line in answer.splitlines():
        ids = [f"E{number}" for number in re.findall(r"\[E(\d+)\]", line) if f"E{number}" in by_evidence]
        if ids:
            statement = re.sub(r"\s*\[[ES]\d+\]", "", line).strip(" -*")
            if statement:
                # Stage 2 records the binding; semantic support is verified in Stage 4.
                claims.append(Claim(f"C{len(claims) + 1}", statement, ids, "UNVERIFIED", None, "awaiting verification"))
    return claims


def validate_citations(answer: str, sources: list[Source]) -> tuple[str, list[str], list[str]]:
    cited = [f"S{number}" for number in re.findall(r"\[S(\d+)\]", answer)]
    known = {source.source_id for source in sources}
    valid = sorted({item for item in cited if item in known}, key=lambda item: (len(item), item))
    invalid = sorted({item for item in cited if item not in known}, key=lambda item: (len(item), item))
    for item in invalid:
        answer = answer.replace(f"[{item}]", "[UNVERIFIED_CITATION]")
    return answer, valid, invalid


def validate_evidence_citations(answer: str, evidence: list[Evidence]) -> tuple[list[str], list[str]]:
    cited = [f"E{number}" for number in re.findall(r"\[E(\d+)\]", answer)]
    known = {item.evidence_id for item in evidence}
    valid = sorted({item for item in cited if item in known}, key=lambda item: (len(item), item))
    invalid = sorted({item for item in cited if item not in known}, key=lambda item: (len(item), item))
    return valid, invalid


def claims_search_without_tool(answer: str) -> bool:
    patterns = (
        r"我(?:已经|已|刚刚)?(?:搜索|查阅|阅读|核验|验证)了",
        r"(?:搜索|检索)结果(?:显示|表明)",
        r"(?:according to|based on) (?:my )?(?:search|sources?)",
        r"\bI\s+(?:have\s+)?(?:searched|checked|verified|looked\s+up)\b",
        r"\bmy\s+search(?:es)?\s+(?:show|found|indicate)\b",
    )
    return any(re.search(pattern, answer, flags=re.IGNORECASE) for pattern in patterns)
