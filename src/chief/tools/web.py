"""chief-owned ``web-fetch`` + ``web-search`` custom tools (#81, part of #72).

The Copilot SDK has no built-in web tools (unlike claude-agent-sdk's ``WebFetch`` /
``WebSearch``), so chief owns them. Built the :mod:`chief.tools.shell` way (``@tool`` +
:func:`create_sdk_mcp_server`), one in-process MCP server named ``chief_web`` reaches
**both** backends automatically: claude-agent-sdk takes the ``mcp_servers`` entry
directly, and :func:`chief.core.copilot_tools.partition_mcp_servers` converts it to a
flat Copilot tool. So the tools are written once, not per backend.

**SSRF guard (security).** Core runs host-native (no sandbox container), so a bare
``httpx.get(url)`` on a model-supplied URL is a real server-side-request-forgery surface
on the owner's machine — it could reach the loopback MCP sidecars, cloud metadata at
``169.254.169.254``, or anything on the LAN. :func:`validate_target` is a
deny-by-default guard: it resolves the host and refuses any loopback / link-local /
private / reserved address, plus the MCP sidecar ports. The connection is then
**pinned** to the validated IP (the request goes to the IP with the original ``Host``
header + SNI) so a DNS rebind between the check and the connect can't swap in a private
address, and every redirect hop is re-validated. A blacklist entry (:mod:`chief.config`)
additionally routes ``fetch`` through an approval card — the guard is the boundary, the
card is defense in depth.

``search`` hits a fixed, trusted provider endpoint (Brave Search) with the owner's API
key, so it needs no SSRF guard; without a key it degrades to a clear "not configured"
note rather than failing.
"""

import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx
from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)

logger = logging.getLogger("chief.tools.web")

#: The SDK names an in-process MCP tool ``mcp__<server>__<tool>`` — the exact strings
#: chief's gate, blacklist, and screening keys off (see :mod:`chief.config`).
SERVER_NAME = "chief_web"
FETCH_TOOL_NAME = f"mcp__{SERVER_NAME}__fetch"
SEARCH_TOOL_NAME = f"mcp__{SERVER_NAME}__search"

#: Loopback ports the MCP sidecars bind (Google calendar/drive/sheets/gmail + the
#: playwright browser). They already sit behind ``is_loopback`` denial, but blocking the
#: ports outright is belt-and-braces per the security review — a rare public service on
#: one of these ports is one shell command away, not a reason to leave the hole open.
_MCP_SIDECAR_PORTS = frozenset({3000, 8001, 8002, 8003, 8004})

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_USER_AGENT = "chief/1.0 (+https://github.com/chief)"

#: (host, port) → list of ``(address_family, ip_string)`` — the resolver seam, injected
#: so tests can drive the guard without real DNS.
Resolver = Callable[[str, int], list[tuple[int, str]]]


class BlockedURLError(Exception):
    """A URL the SSRF guard refused (bad scheme, or a blocked resolved address)."""


def _default_resolver(host: str, port: int) -> list[tuple[int, str]]:
    """Resolve ``host`` to every ``(family, ip)`` it maps to (real DNS)."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [(family, str(sockaddr[0])) for family, _, _, _, sockaddr in infos]


def _is_blocked_ip(ip_str: str) -> bool:
    """True if ``ip_str`` is loopback/link-local/private/reserved and must be refused.

    An unparseable string fails closed (blocked). An IPv4-mapped IPv6 address
    (``::ffff:127.0.0.1``) is unwrapped so a mapped loopback can't slip past.
    """
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            ip_str
        )
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


@dataclass(frozen=True)
class ValidatedTarget:
    """A URL that passed the guard, with the resolved IPs to pin the connect to."""

    host: str
    port: int
    ips: tuple[str, ...]


def validate_target(
    url: str,
    *,
    resolver: Resolver = _default_resolver,
    blocked_ports: frozenset[int] = _MCP_SIDECAR_PORTS,
) -> ValidatedTarget:
    """Resolve ``url`` and refuse it unless every resolved address is public.

    Deny-by-default: a non-http(s) scheme, a missing host, a blocked port, an
    unresolvable host, or *any* resolved address in a loopback/link-local/private/
    reserved range raises :class:`BlockedURLError`. Returns the validated IPs so the
    caller can pin the connection to them (DNS-rebinding defense).
    """
    parsed = httpx.URL(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(
            f"scheme {parsed.scheme!r} is not allowed (only http/https)"
        )
    host = parsed.host
    if not host:
        raise BlockedURLError("URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port in blocked_ports:
        raise BlockedURLError(
            f"port {port} is blocked (reserved for chief's internal services)"
        )
    infos = resolver(host, port)
    if not infos:
        raise BlockedURLError(f"could not resolve host {host!r}")
    ips: list[str] = []
    for _family, ip_str in infos:
        if _is_blocked_ip(ip_str):
            raise BlockedURLError(
                f"{host!r} resolves to blocked address {ip_str} "
                "(loopback/link-local/private/reserved)"
            )
        ips.append(ip_str)
    return ValidatedTarget(host=host, port=port, ips=tuple(ips))


class _TextExtractor(HTMLParser):
    """Collapse HTML to readable text, dropping ``<script>`` / ``<style>`` bodies."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self._parts)


def _render(body: bytes, content_type: str) -> str:
    """Decode a response body to text; strip tags when it looks like HTML."""
    text = body.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        parser = _TextExtractor()
        parser.feed(text)
        return parser.text()
    return text


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """An MCP tool result carrying one text block (mirrors other chief tools)."""
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


@dataclass
class WebFetcher:
    """Fetches a URL behind the SSRF guard, pinning the connect to a validated IP.

    ``resolver`` and ``transport`` are injectable seams so the guard + redirect logic
    are exercised offline; the real path uses system DNS and httpx's default transport.
    """

    resolver: Resolver = _default_resolver
    timeout: float = 15.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    blocked_ports: frozenset[int] = _MCP_SIDECAR_PORTS
    transport: httpx.AsyncBaseTransport | None = None

    async def fetch(self, url: str) -> str:
        """Return the text content at ``url`` (HTML stripped), guarded end-to-end.

        Redirects are followed manually so every hop is re-validated by
        :func:`validate_target`; :class:`BlockedURLError` propagates for the caller to
        surface. Raises :class:`BlockedURLError` if the redirect budget is exhausted.
        """
        current = url
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout,
            follow_redirects=False,
            verify=True,
        ) as client:
            for _ in range(self.max_redirects + 1):
                target = validate_target(
                    current,
                    resolver=self.resolver,
                    blocked_ports=self.blocked_ports,
                )
                resp = await self._send(client, current, target)
                try:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return _render(
                                await self._read(resp),
                                resp.headers.get("content-type", ""),
                            )
                        current = str(httpx.URL(current).join(location))
                        continue
                    return _render(
                        await self._read(resp),
                        resp.headers.get("content-type", ""),
                    )
                finally:
                    await resp.aclose()
        raise BlockedURLError(f"too many redirects (>{self.max_redirects})")

    async def _send(
        self, client: httpx.AsyncClient, url: str, target: ValidatedTarget
    ) -> httpx.Response:
        """Send a GET pinned to a validated IP, keeping the real Host + SNI.

        Connecting to the pre-validated IP (rather than letting httpx re-resolve)
        closes the DNS-rebinding window between :func:`validate_target` and the connect.
        """
        parsed = httpx.URL(url)
        connect_url = parsed.copy_with(host=target.ips[0])
        request = client.build_request(
            "GET",
            connect_url,
            headers={"Host": target.host, "User-Agent": _USER_AGENT},
            extensions={"sni_hostname": target.host},
        )
        return await client.send(request, stream=True)

    async def _read(self, resp: httpx.Response) -> bytes:
        """Read the body up to ``max_bytes`` so a huge page can't exhaust memory."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= self.max_bytes:
                break
        return b"".join(chunks)[: self.max_bytes]


def _format_brave(data: dict[str, Any], limit: int) -> str:
    """Render Brave's web-search JSON as a compact numbered result list."""
    results = (data.get("web") or {}).get("results") or []
    if not results:
        return "No results found."
    lines: list[str] = []
    for i, item in enumerate(results[:limit], start=1):
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        desc = str(item.get("description", "")).strip()
        lines.append(f"{i}. {title}\n   {url}\n   {desc}")
    return "\n".join(lines)


@dataclass
class BraveSearcher:
    """Runs a web search via the Brave Search API (the chosen provider).

    A fixed, trusted endpoint, so no SSRF guard applies. With no ``api_key`` the search
    tool degrades to a clear note rather than erroring — ``fetch`` still works without a
    provider key.
    """

    api_key: str | None = None
    endpoint: str = "https://api.search.brave.com/res/v1/web/search"
    count: int = 5
    timeout: float = 15.0
    transport: httpx.AsyncBaseTransport | None = None

    async def search(self, query: str) -> str:
        if not self.api_key:
            return (
                "Web search is not configured — set the brave_search_api_key secret "
                "to enable it."
            )
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout
        ) as client:
            resp = await client.get(
                self.endpoint,
                params={"q": query, "count": self.count},
                headers={
                    "X-Subscription-Token": self.api_key,
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return _format_brave(data, self.count)


_FETCH_DESCRIPTION = (
    "Fetch the readable text content of a web page by URL (http/https). Use this to "
    "read an article, doc, or any page the owner links or you found via search. "
    "Private, loopback, and link-local addresses are refused for safety, and each "
    "fetch may ask the owner for approval."
)

_SEARCH_DESCRIPTION = (
    "Search the web and get back a ranked list of results (title, URL, snippet). Use "
    "this to find current information or pages to then fetch. Follow up with the fetch "
    "tool to read a result in full."
)


@dataclass
class WebService:
    """Builds the owner session's ``chief_web`` MCP server (fetch + search).

    Owner-only (never wired into a guest session), mirroring
    :class:`~chief.tools.shell.ShellService`: ``tasks.py`` registers
    :meth:`server_config` in the owner ``mcp_servers`` mapping, which reaches both the
    claude and copilot backends via the #80 adapter.
    """

    fetcher: WebFetcher = field(default_factory=WebFetcher)
    searcher: BraveSearcher = field(default_factory=BraveSearcher)
    server_name: str = SERVER_NAME

    @property
    def fetch_tool_name(self) -> str:
        """The ``mcp__chief_web__fetch`` name (the blacklist/screening key)."""
        return f"mcp__{self.server_name}__fetch"

    @property
    def search_tool_name(self) -> str:
        """The SDK-qualified ``mcp__chief_web__search`` name (screening key)."""
        return f"mcp__{self.server_name}__search"

    def _build_fetch_tool(self) -> SdkMcpTool[Any]:
        fetcher = self.fetcher

        @tool("fetch", _FETCH_DESCRIPTION, {"url": str})
        async def fetch(args: dict[str, Any]) -> dict[str, Any]:
            url = str(args.get("url", "")).strip()
            if not url:
                return _text_result("No URL provided.", is_error=True)
            try:
                content = await fetcher.fetch(url)
            except BlockedURLError as exc:
                return _text_result(f"Refused to fetch {url}: {exc}", is_error=True)
            except httpx.HTTPError as exc:
                return _text_result(f"Failed to fetch {url}: {exc}", is_error=True)
            return _text_result(content or "(the page returned no readable text)")

        return fetch

    def _build_search_tool(self) -> SdkMcpTool[Any]:
        searcher = self.searcher

        @tool("search", _SEARCH_DESCRIPTION, {"query": str})
        async def search(args: dict[str, Any]) -> dict[str, Any]:
            query = str(args.get("query", "")).strip()
            if not query:
                return _text_result("No search query provided.", is_error=True)
            try:
                results = await searcher.search(query)
            except httpx.HTTPError as exc:
                return _text_result(f"Search failed: {exc}", is_error=True)
            return _text_result(results)

        return search

    def server_config(self) -> McpSdkServerConfig:
        """The in-process ``mcp_servers`` entry for the fetch + search tools."""
        return create_sdk_mcp_server(
            self.server_name,
            tools=[self._build_fetch_tool(), self._build_search_tool()],
        )
