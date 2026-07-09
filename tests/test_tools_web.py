"""chief-owned web-fetch + web-search tools (#81, part of #72).

Exercises the real central mechanism: the SSRF guard resolving + refusing addresses, a
real fetch through httpx (pinned to the validated IP, redirects re-validated) driven by
an injected ``MockTransport``, the Brave search formatter, and — the acceptance
criterion "under CopilotBackend" — the tools invoked through the #80 Copilot adapter
(:func:`chief.core.copilot_tools.sdk_server_to_tools`), so they run under the Copilot
tool shape, not a stand-in.
"""

import inspect
import socket
from typing import Any

import httpx
import pytest
from copilot import Tool, ToolInvocation, ToolResult

from chief.core.copilot_tools import sdk_server_to_tools
from chief.tools.web import (
    FETCH_TOOL_NAME,
    SEARCH_TOOL_NAME,
    BlockedURLError,
    BraveSearcher,
    WebFetcher,
    WebService,
    validate_target,
)


def _resolver(*ips: str) -> Any:
    """A resolver seam that maps any host to the given IP(s) (no real DNS)."""

    def resolve(host: str, port: int) -> list[tuple[int, str]]:
        return [(socket.AF_INET, ip) for ip in ips]

    return resolve


# ---- SSRF guard -------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback (the MCP sidecars bind here)
        "169.254.169.254",  # cloud metadata (link-local)
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "172.16.5.4",  # private
        "::1",  # IPv6 loopback
    ],
)
def test_guard_refuses_internal_addresses(ip: str) -> None:
    with pytest.raises(BlockedURLError):
        validate_target("https://target.example/", resolver=_resolver(ip))


def test_guard_refuses_dns_rebinding_to_private() -> None:
    # A public-looking hostname that RESOLVES to a private address is refused — the
    # guard validates the resolved IP, not the hostname string (rebinding defense).
    with pytest.raises(BlockedURLError, match="blocked address"):
        validate_target(
            "https://totally-public.example/", resolver=_resolver("10.1.2.3")
        )


def test_guard_refuses_mcp_sidecar_port() -> None:
    # Even on a public IP, the sidecar ports are blocked (belt-and-braces).
    with pytest.raises(BlockedURLError, match="port 8001"):
        validate_target("http://ok.example:8001/", resolver=_resolver("93.184.216.34"))


def test_guard_refuses_non_http_scheme() -> None:
    with pytest.raises(BlockedURLError, match="scheme"):
        validate_target("file:///etc/passwd", resolver=_resolver("93.184.216.34"))


def test_guard_allows_public_address() -> None:
    target = validate_target(
        "https://example.com/page", resolver=_resolver("93.184.216.34")
    )
    assert target.host == "example.com"
    assert target.port == 443
    assert target.ips == ("93.184.216.34",)


# ---- fetch ------------------------------------------------------------------


async def test_fetch_pins_to_validated_ip_and_strips_html() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("host")))
        body = b"<html><body><script>bad()</script>Hello <b>world</b></body></html>"
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=body
        )

    fetcher = WebFetcher(
        resolver=_resolver("93.184.216.34"),
        transport=httpx.MockTransport(handler),
    )

    text = await fetcher.fetch("https://example.com/page")

    assert "Hello" in text and "world" in text
    assert "bad()" not in text  # <script> body dropped
    # Connection was pinned to the validated IP with the real Host preserved.
    assert seen == [("93.184.216.34", "example.com")]


async def test_fetch_revalidates_redirect_target() -> None:
    # A redirect whose Location resolves to a private address is refused on the 2nd hop.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301, headers={"location": "https://intranet.example/secret"}
        )

    def resolver(host: str, port: int) -> list[tuple[int, str]]:
        ip = "10.0.0.9" if host == "intranet.example" else "93.184.216.34"
        return [(socket.AF_INET, ip)]

    fetcher = WebFetcher(resolver=resolver, transport=httpx.MockTransport(handler))

    with pytest.raises(BlockedURLError, match="blocked address"):
        await fetcher.fetch("https://public.example/start")


async def test_fetch_follows_allowed_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "https://public.example/final"}
            )
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"final page"
        )

    fetcher = WebFetcher(
        resolver=_resolver("93.184.216.34"),
        transport=httpx.MockTransport(handler),
    )

    text = await fetcher.fetch("https://public.example/start")

    assert text == "final page"


async def test_fetch_caps_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"x" * 10_000
        )

    fetcher = WebFetcher(
        resolver=_resolver("93.184.216.34"),
        transport=httpx.MockTransport(handler),
        max_bytes=100,
    )

    text = await fetcher.fetch("https://example.com/big")

    assert len(text) == 100


# ---- search -----------------------------------------------------------------


async def test_search_formats_brave_results() -> None:
    payload = {
        "web": {
            "results": [
                {
                    "title": "Chief docs",
                    "url": "https://example.com/docs",
                    "description": "All about chief.",
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-subscription-token"] == "brave-key"
        assert request.url.params["q"] == "chief assistant"
        return httpx.Response(200, json=payload)

    searcher = BraveSearcher(
        api_key="brave-key", transport=httpx.MockTransport(handler)
    )

    out = await searcher.search("chief assistant")

    assert "Chief docs" in out
    assert "https://example.com/docs" in out


async def test_search_without_key_degrades() -> None:
    searcher = BraveSearcher(api_key=None)
    out = await searcher.search("anything")
    assert "not configured" in out.lower()


# ---- reaches the Copilot backend (acceptance criterion) ---------------------


async def _invoke(tool: Tool, **arguments: Any) -> ToolResult:
    assert tool.handler is not None
    result = tool.handler(ToolInvocation(arguments=arguments))
    return await result if inspect.isawaitable(result) else result


def _web_service(handler: Any) -> WebService:
    return WebService(
        fetcher=WebFetcher(
            resolver=_resolver("93.184.216.34"),
            transport=httpx.MockTransport(handler),
        ),
        searcher=BraveSearcher(
            api_key="brave-key", transport=httpx.MockTransport(handler)
        ),
    )


async def _tools(service: WebService) -> dict[str, Tool]:
    tools = await sdk_server_to_tools(service.server_config())
    return {t.name: t for t in tools}


async def test_fetch_and_search_run_under_copilot_backend() -> None:
    # AC: chief fetches a URL and runs a web search via the custom tools under the
    # Copilot backend — the tools convert to Copilot tools and their real handlers run.
    def handler(request: httpx.Request) -> httpx.Response:
        if "search" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Result",
                                "url": "https://example.com/",
                                "description": "desc",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<p>page body</p>"
        )

    tools = await _tools(_web_service(handler))
    assert set(tools) == {FETCH_TOOL_NAME, SEARCH_TOOL_NAME}

    fetched = await _invoke(tools[FETCH_TOOL_NAME], url="https://example.com/page")
    assert fetched.result_type == "success"
    assert "page body" in fetched.text_result_for_llm

    searched = await _invoke(tools[SEARCH_TOOL_NAME], query="chief")
    assert searched.result_type == "success"
    assert "Result" in searched.text_result_for_llm


async def test_fetch_tool_refuses_blocked_url_under_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("blocked URL must never reach the network")

    service = WebService(
        fetcher=WebFetcher(
            resolver=_resolver("127.0.0.1"),
            transport=httpx.MockTransport(handler),
        ),
    )
    tools = await _tools(service)

    result = await _invoke(tools[FETCH_TOOL_NAME], url="https://sneaky.example/")

    assert result.result_type == "failure"
    assert "Refused" in result.text_result_for_llm
