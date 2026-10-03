"""Search and read adapters for Stage 2."""

from __future__ import annotations

import http.client
import hashlib
import ipaddress
import json
import re
import socket
import ssl
import zlib
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .contracts import ReadResponse, SearchProvider, SearchResponse
from .trace import is_sensitive_key, now_iso, redact, shorten


def _has_sensitive_parameter(value: str) -> bool:
    try:
        return any(is_sensitive_key(key) for key, _ in parse_qsl(value.replace(";", "&"), keep_blank_values=True, max_num_fields=100))
    except ValueError:
        return True


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    host = host.strip("[]")
    if "%" in host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[0-9.]+", host):
            return None
        try:
            return ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            return None


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not any((ip.is_loopback, ip.is_link_local, ip.is_multicast, ip.is_private, ip.is_reserved, ip.is_unspecified))


def _blocked_host(host: str) -> bool:
    normalized = host.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    literal = _literal_ip(normalized)
    return literal is not None and not _public_ip(str(literal))


def canonical_url(value: str) -> str | None:
    """Return the narrow set of URL equivalences accepted for read authorization."""
    if (
        not value
        or len(value) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char.isspace() for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not hostname
            or hostname == "."
            or parsed.username is not None
            or parsed.password is not None
            or _has_sensitive_parameter(parsed.query)
            or _has_sensitive_parameter(parsed.fragment)
        ):
            return None
        host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or _blocked_host(host):
            return None
        host_part = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if scheme == "https" else 80):
            host_part += f":{port}"
        return urlunsplit((scheme, host_part, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, ValueError):
        return None


def valid_http_url(value: str) -> bool:
    return canonical_url(value) is not None


def url_rejection_reason(value: str) -> str:
    """Classify unsafe URLs without returning credential-bearing input."""
    try:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None or _has_sensitive_parameter(parsed.query) or _has_sensitive_parameter(parsed.fragment):
            return "credential_url"
        if parsed.hostname and _blocked_host(parsed.hostname):
            return "network_target_denied"
    except ValueError:
        pass
    return "invalid_url"


class UnsafeNetworkTarget(ValueError):
    pass


def resolve_public_addresses(host: str, port: int, resolver=socket.getaddrinfo) -> list[tuple]:
    """Resolve once and require every candidate address to be globally routable."""
    if _blocked_host(host):
        raise UnsafeNetworkTarget("private, local, or reserved network target")
    try:
        addresses = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OSError(f"DNS resolution failed: {exc}") from exc
    if not addresses or any(not _public_ip(str(item[4][0])) for item in addresses):
        raise UnsafeNetworkTarget("DNS returned a private, local, or reserved address")
    unique: list[tuple] = []
    seen: set[tuple] = set()
    for item in addresses:
        key = (item[0], item[1], item[2], item[4])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _connect_pinned(addresses: list[tuple], timeout: float) -> socket.socket:
    expected = {str(item[4][0]).split("%", 1)[0] for item in addresses}
    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            peer = str(sock.getpeername()[0]).split("%", 1)[0]
            if peer not in expected or not _public_ip(peer):
                raise UnsafeNetworkTarget("connected peer differs from the validated public address")
            return sock
        except UnsafeNetworkTarget:
            sock.close()
            raise
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("could not connect to a validated address")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: list[tuple], timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._addresses = addresses

    def connect(self) -> None:
        self.sock = _connect_pinned(self._addresses, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, addresses: list[tuple], timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._addresses = addresses

    def connect(self) -> None:
        raw = _connect_pinned(self._addresses, self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        peer = str(self.sock.getpeername()[0]).split("%", 1)[0]
        if not _public_ip(peer):
            self.sock.close()
            raise UnsafeNetworkTarget("TLS peer is not a public address")


@dataclass
class _NetworkResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False


def _media_type(value: str) -> tuple[str, str]:
    if not value.strip():
        return "", "utf-8"
    message = Message()
    message["content-type"] = value
    return message.get_content_type().lower(), message.get_content_charset() or "utf-8"


class _ReadableHTMLParser(HTMLParser):
    """Keep useful document blocks and code while discarding page chrome."""

    _BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "code"}
    _BREAKS = {"article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure", "header", "main", "ol", "section", "table", "td", "th", "tr", "ul"}
    _CONTENT_ROOTS = {"article", "main"}
    _IGNORED = {"script", "style", "nav", "footer", "aside", "noscript", "svg", "template"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.blocks: list[str] = []
        self._ignored: list[str] = []
        self._in_title = False
        self._active_tag = ""
        self._active_depth = 0
        self._active_parts: list[str] = []
        self._loose_parts: list[str] = []
        self._content_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.append(tag)
            return
        hidden = any(name.casefold() == "hidden" for name, _ in attrs)
        if tag in self._IGNORED or (tag == "header" and not self._content_depth) or hidden:
            self._flush_loose()
            if tag not in self._VOID:
                self._ignored.append(tag)
            return
        if tag in self._CONTENT_ROOTS:
            self._content_depth += 1
        if tag == "title":
            self._in_title = True
        elif not self._active_tag and tag in self._BLOCKS:
            self._flush_loose()
            self._active_tag = tag
            self._active_depth = 1
            self._active_parts = []
        elif self._active_tag and tag == self._active_tag:
            self._active_depth += 1
            self._active_parts.append("\n")
        elif self._active_tag and (tag in self._BLOCKS or tag in self._BREAKS):
            self._active_parts.append("\n")
        elif tag in self._BREAKS:
            self._flush_loose()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag == "title":
            self._in_title = False
        elif tag == self._active_tag:
            if self._active_depth > 1:
                self._active_depth -= 1
                self._active_parts.append("\n")
            else:
                self._flush_block()
        elif self._active_tag and (tag in self._BLOCKS or tag in self._BREAKS):
            self._active_parts.append("\n")
        elif tag in self._BREAKS:
            self._flush_loose()
        if tag in self._CONTENT_ROOTS and self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif self._active_tag:
            self._active_parts.append(data)
        else:
            self._loose_parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush_block()
        self._flush_loose()

    def _flush_block(self) -> None:
        if not self._active_tag:
            return
        tag, raw = self._active_tag, "".join(self._active_parts)
        if tag in {"pre", "code"}:
            text = raw.strip("\r\n")
            block = f"```\n{text}\n```" if text.strip() else ""
        else:
            text = " ".join(raw.split())
            prefix = "#" * int(tag[1]) + " " if tag.startswith("h") else ("- " if tag == "li" else "")
            block = prefix + text if text else ""
        if block:
            self.blocks.append(block)
        self._active_tag = ""
        self._active_depth = 0
        self._active_parts = []

    def _flush_loose(self) -> None:
        text = " ".join("".join(self._loose_parts).split())
        if text:
            self.blocks.append(text)
        self._loose_parts = []

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())

    @property
    def content(self) -> str:
        return "\n\n".join(self.blocks).strip()


def extract_html_text(value: str) -> tuple[str, str]:
    parser = _ReadableHTMLParser()
    parser.feed(value)
    parser.close()
    return parser.title, parser.content


def summarize_content(value: str, limit: int = 1600) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[summary truncated]"


def _text_response(
    original_url: str,
    final_url: str,
    text: str,
    media_type: str,
    network_requests: int,
    truncated: bool,
) -> ReadResponse:
    title, content = extract_html_text(text) if media_type == "text/html" else ("", text.strip())
    # A clipped HTML header is not a successfully read article.
    navigation_only = bool(content) and all(re.fullmatch(
        r"(?:skip to (?:main )?content|(?:collapse|expand) sidebar|scroll breadcrumbs (?:left|right))",
        line.strip(), re.I) for line in content.splitlines() if line.strip())
    if not content or navigation_only or (media_type == "text/html" and truncated):
        return ReadResponse(False, original_url, error={"code": "incomplete_body",
            "message": "No complete readable body: empty/navigation-only content or HTML download limit reached. Find an accessible original or report the reading gap."},
            network_requests=network_requests, media_type=media_type, final_url=final_url,
            title=title, truncated=truncated)
    return ReadResponse(
        ok=True,
        url=original_url,
        content=content,
        network_requests=network_requests,
        media_type=media_type,
        final_url=final_url,
        summary=summarize_content(content),
        title=title,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        truncated=truncated,
        retrieved_at=now_iso(),
    )


def _request_once(url: str, addresses: list[tuple], timeout: float, max_bytes: int, accepted_types=None) -> _NetworkResponse:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    connection = connection_type(host, port, addresses, timeout)
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        accepted_types = accepted_types or {"text/html", "text/plain"}
        connection.request("GET", target, headers={"Accept": ",".join(sorted(accepted_types)), "Accept-Encoding": "identity", "User-Agent": "evidence-agent/0.2"})
        response = connection.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        media_type, _ = _media_type(headers.get("content-type", ""))
        should_read = 200 <= response.status < 300 and media_type in accepted_types
        body = response.read(max_bytes + 1) if should_read else b""
        return _NetworkResponse(response.status, headers, body[:max_bytes], len(body) > max_bytes)
    finally:
        connection.close()


def _tokens(text: str) -> set[str]:
    return {item.lower() for item in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text)}


class FixtureSearch:
    """A deterministic search provider backed by a JSON list."""

    def __init__(self, records: Iterable[dict[str, Any]]):
        self.records = [record for record in records if isinstance(record, dict)]

    @classmethod
    def from_file(cls, path: Path) -> "FixtureSearch":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("fixture must be a JSON list")
        return cls(data)

    def search(self, query: str) -> SearchResponse:
        query = query.strip()
        if not query:
            return SearchResponse(False, query, error={"code": "empty_query", "message": "query is empty"})
        query_tokens = _tokens(query)
        ranked: list[tuple[int, dict[str, Any]]] = []
        for record in self.records:
            text = " ".join(str(record.get(name, "")) for name in ("title", "snippet", "content", "publisher", "aliases"))
            score = len(query_tokens & _tokens(text))
            if score:
                ranked.append((score, record))
        ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("title", ""))))
        return SearchResponse(True, query, [dict(record) for _, record in ranked[:5]])


class FixtureReader:
    def __init__(self, records: Iterable[dict[str, Any]]):
        self.records = {str(item.get("url", "")): item for item in records if isinstance(item, dict)}

    @classmethod
    def from_file(cls, path: Path) -> "FixtureReader":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data if isinstance(data, list) else [])

    def read(self, url: str) -> ReadResponse:
        if not valid_http_url(url):
            return ReadResponse(False, url, error={"code": "invalid_url", "message": "invalid HTTP(S) URL"})
        item = self.records.get(url)
        if item is None:
            return ReadResponse(False, url, error={"code": "not_in_fixture", "message": "URL is not present in offline fixture"})
        media_type = str(item.get("media_type") or "text/plain").split(";", 1)[0].strip().lower()
        return _text_response(
            url,
            url,
            str(item.get("content") or item.get("snippet") or ""),
            media_type,
            0,
            bool(item.get("truncated", False)),
        )


class FailingSearch:
    def __init__(self, message: str = "simulated search timeout"):
        self.message = message

    def search(self, query: str) -> SearchResponse:
        return SearchResponse(False, query, error={"code": "search_failed", "message": self.message})


class JsonSearch:
    """Optional HTTP adapter. The endpoint should return ``{"results": [...]}``."""

    def __init__(self, endpoint: str, api_key: str | None = None, timeout: float = 15.0):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("SEARCH_URL must be an http(s) URL")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def search(self, query: str) -> SearchResponse:
        separator = "&" if "?" in self.endpoint else "?"
        url = f"{self.endpoint}{separator}q={quote_plus(query)}"
        headers = {"Accept": "application/json", "User-Agent": "evidence-agent/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(results, list):
                raise ValueError("search response results must be a list")
            return SearchResponse(True, query, [dict(item) for item in results if isinstance(item, dict)], network_requests=1)
        except HTTPError as exc:
            code = "search_transient" if exc.code in {408, 425, 429} or exc.code >= 500 else "search_http_status"
            return SearchResponse(False, query, error={"code": code, "message": f"HTTP {exc.code}"}, network_requests=1)
        except (URLError, TimeoutError, OSError) as exc:
            return SearchResponse(False, query, error={"code": "search_transient", "message": shorten(redact(exc), 500)}, network_requests=1)
        except (UnicodeError, ValueError) as exc:
            return SearchResponse(False, query, error={"code": "search_invalid_response", "message": shorten(redact(exc), 500)}, network_requests=1)


class TavilySearch:
    """Tavily search adapter using the public JSON API and stdlib urllib."""

    def __init__(self, api_key: str, endpoint: str = "https://api.tavily.com/search", timeout: float = 30.0):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("TAVILY_URL must be an http(s) URL")
        if not api_key.strip():
            raise ValueError("TAVILY_API_KEY must not be empty")
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self.timeout = timeout

    def search(self, query: str) -> SearchResponse:
        query = query.strip()
        if not query:
            return SearchResponse(False, query, error={"code": "empty_query", "message": "query is empty"})
        body = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": False,
        }
        domains = []
        for match in re.finditer(r"(?:^|[\s(])site:([a-z0-9][a-z0-9.-]*)(?=[/\s)]|$)", query, re.I):
            domain = match[1].lower().rstrip('.')
            if '.' in domain and canonical_url('https://' + domain) and domain not in domains:
                domains.append(domain)
        if domains:
            body["include_domains"] = domains
        request = Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "evidence-agent/0.1"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            results = payload.get("results", []) if isinstance(payload, dict) else []
            if not isinstance(results, list):
                raise ValueError("Tavily response results must be a list")
            normalized = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                normalized.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", "") or item.get("snippet", ""),
                    "score": item.get("score"),
                })
            return SearchResponse(True, query, normalized, network_requests=1)
        except HTTPError as exc:
            code = "search_transient" if exc.code in {408, 425, 429} or exc.code >= 500 else "search_http_status"
            return SearchResponse(False, query, error={"code": code, "message": f"HTTP {exc.code}"}, network_requests=1)
        except (URLError, TimeoutError, OSError) as exc:
            return SearchResponse(False, query, error={"code": "search_transient", "message": shorten(redact(exc), 500)}, network_requests=1)
        except (UnicodeError, ValueError) as exc:
            return SearchResponse(False, query, error={"code": "search_invalid_response", "message": shorten(redact(exc), 500)}, network_requests=1)


class ArxivSearch:
    """Scope an existing search provider, and enforce the returned host boundary."""

    def __init__(self, provider: SearchProvider):
        self.provider = provider

    def search(self, query: str) -> SearchResponse:
        query = "site:arxiv.org " + re.sub(r"\bsite:\S+", "", query, flags=re.I).strip()[:285]
        response = self.provider.search(query)
        if not response.ok:
            return response
        results = []
        for item in response.results:
            if not isinstance(item, dict):
                continue
            url = canonical_url(str(item.get("url", "")))
            host = urlsplit(url).hostname if url else None
            if (host == "arxiv.org" or (host and host.endswith(".arxiv.org"))) and re.match(r"^/(abs|html|pdf)/.+", urlsplit(url).path):
                results.append(item)
        return SearchResponse(True, query, results, network_requests=response.network_requests)


class HttpReader:
    """Read public HTML/text while pinning each request to checked addresses."""

    def __init__(self, timeout: float = 20.0, max_bytes: int = 2 * 1024 * 1024, max_redirects: int = 3):
        if timeout <= 0 or max_bytes < 1 or max_redirects < 0:
            raise ValueError("timeout/max_bytes must be positive and max_redirects non-negative")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects

    def read(self, url: str) -> ReadResponse:
        original_url = url
        current = canonical_url(url)
        if current is None:
            reason = url_rejection_reason(url)
            code = "network_target_denied" if reason == "network_target_denied" else reason
            return ReadResponse(False, url, error={"code": code, "message": "only credential-free public HTTP(S) URLs are allowed"})
        network_requests = 0
        try:
            for redirect_count in range(self.max_redirects + 1):
                parsed = urlsplit(current)
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                addresses = resolve_public_addresses(parsed.hostname or "", port)
                network_requests += 1
                response = _request_once(current, addresses, self.timeout, self.max_bytes)
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    target = canonical_url(urljoin(current, location)) if location else None
                    if target is None:
                        return ReadResponse(False, original_url, error={"code": "unsafe_redirect", "message": "redirect target is missing or unsafe"}, network_requests=network_requests)
                    if redirect_count == self.max_redirects:
                        return ReadResponse(False, original_url, error={"code": "too_many_redirects", "message": "redirect limit exceeded"}, network_requests=network_requests)
                    current = target
                    continue
                if not 200 <= response.status < 300:
                    code = "read_transient" if response.status in {408, 425, 429} or response.status >= 500 else "http_status"
                    return ReadResponse(False, original_url, error={"code": code, "message": f"HTTP {response.status}"}, network_requests=network_requests, final_url=current)
                media_type, charset = _media_type(response.headers.get("content-type", ""))
                if media_type not in {"text/html", "text/plain"}:
                    return ReadResponse(False, original_url, error={"code": "unsupported_media_type", "message": media_type or "missing Content-Type"}, network_requests=network_requests, media_type=media_type, final_url=current)
                body, truncated = response.body, response.truncated
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if encoding not in {"", "identity", "gzip", "x-gzip"}:
                    return ReadResponse(False, original_url, error={"code": "unsupported_content_encoding", "message": encoding}, network_requests=network_requests, final_url=current)
                # Some publisher pages return gzip bytes without a Content-Encoding header.
                if encoding in {"gzip", "x-gzip"} or body.startswith(b"\x1f\x8b"):
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    body = decoder.decompress(body, self.max_bytes + 1)
                    truncated = truncated or len(body) > self.max_bytes or bool(decoder.unused_data)
                    if not decoder.eof and not truncated:
                        raise zlib.error("Incomplete gzip response")
                    body = body[:self.max_bytes]
                return _text_response(
                    original_url,
                    current,
                    body.decode(charset, errors="replace"),
                    media_type,
                    network_requests,
                    truncated,
                )
        except UnsafeNetworkTarget as exc:
            return ReadResponse(False, original_url, error={"code": "network_target_denied", "message": str(exc)}, network_requests=network_requests, final_url=current)
        except (http.client.HTTPException, HTTPError, URLError, TimeoutError, OSError) as exc:
            return ReadResponse(False, original_url, error={"code": "read_transient", "message": shorten(redact(exc), 500)}, network_requests=network_requests, final_url=current)
        except (UnicodeError, LookupError, zlib.error) as exc:
            return ReadResponse(False, original_url, error={"code": "invalid_text_encoding", "message": shorten(redact(exc), 500)}, network_requests=network_requests, final_url=current)
        return ReadResponse(False, original_url, error={"code": "too_many_redirects", "message": "redirect limit exceeded"}, network_requests=network_requests, final_url=current)
