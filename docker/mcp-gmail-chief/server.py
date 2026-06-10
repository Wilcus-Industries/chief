"""chief's own Google Gmail MCP server — FastMCP over streamable-HTTP.

Replaces the third-party ``mcp-google-gmail`` container (cutover in issue #52). A thin
FastMCP server on top of ``google-api-python-client``, mirroring the pattern established
by ``docker/mcp-calendar/server.py``.

Auth — multi-account (issue #48, per-request re-scan issue #60):
    The server re-scans TOKEN_DIR (default ``/token``) on every request so a token
    dropped after startup is picked up immediately — no restart needed.  On each
    ``tools/call``, ``_get_service()`` reads ``X-Account-Label`` directly from the
    Starlette request object exposed via FastMCP's per-call ``request_context``
    (``mcp.get_context().request_context.request``).  This is genuinely per-request
    scoped — the MCP lowlevel server sets the ``request_ctx`` ContextVar *inside* the
    spawned task that handles each message, so it is never frozen at session-start.
    Falls back to the first (default) credential when no header is present.

    See ``docker/mcp-calendar/server.py`` for the full explanation of why ContextVar
    middleware does NOT work here (stateful-session anyio boundary).

Signature (issue #48 scaffold, wired in #51):
    The transparent "sent by an assistant" signature is appended server-side to every
    outbound body. ``gmail_send_message`` / ``gmail_reply_on_message`` call
    ``inject_signature`` (from ``gmail_signature``) before building the RFC822 message,
    so the marking is added independent of what the model wrote.

MIME body walk:
    ``_extract_body`` walks the base64url-encoded ``parts`` tree of a Gmail message
    payload, preferring text/plain over text/html, recursing into multipart subtypes.

Tool names are snake_case (``@mcp.tool(name=...)``) to match the tool catalog in
``src/chief/tools/gmail/mcp.py``.  This file is standalone: imports nothing from
``chief`` and ships in its own image with its own ``requirements.txt``.

Reuse pattern (calendar → gmail):
    ``_build_service_registry()`` / ``_get_service_for_label()`` / ``_get_service()``
    mirror calendar's per-request re-scan pattern (issue #50/#60).
"""

import asyncio
import base64
import json
import logging
import os
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from gmail_signature import inject_signature

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("gmail.server")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]
#: Legacy single-token path (backward compat).
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/token/google_token.json")
#: Directory scanned for all ``google_token*.json`` files.
TOKEN_DIR = os.environ.get("TOKEN_DIR", str(Path(TOKEN_PATH).parent))
PORT = int(os.environ.get("PORT", "8005"))

#: Header name chief stamps with the thread's active account label.
#: ASGI lower-cases incoming header names; match the normalised form.
_ACCOUNT_HEADER = "x-account-label"

#: The transparent signature appended to outbound messages (scaffold — used by #51/#52).
_OWNER = os.environ.get("OWNER_NAME") or "the owner"
_RAW_SIGNATURE = (
    os.environ.get("GMAIL_SIGNATURE")
    or "—\nSent by {owner}'s assistant on their behalf."
)
try:
    SIGNATURE = _RAW_SIGNATURE.format(owner=_OWNER)
except (KeyError, IndexError, ValueError):
    SIGNATURE = _RAW_SIGNATURE

# ---------------------------------------------------------------------------
# Multi-account credential registry
# ---------------------------------------------------------------------------

_TOKEN_PREFIX = "google_token"
_LEGACY_NAME = "google_token.json"


def _load_credentials(token_dir: Path) -> dict[str, Any]:
    """Scan ``token_dir`` for ``google_token*.json`` files.

    Returns a label → Credentials dict.  The legacy ``google_token.json`` (if present)
    is always the first entry (default fallback).  Credentials are loaded but NOT
    refreshed here; google-api-python-client refreshes in memory on the first API call.
    """
    if not token_dir.is_dir():
        return {}
    registry: dict[str, Any] = {}
    legacy: list[tuple[str, Any]] = []
    labeled: list[tuple[str, Any]] = []
    for path in sorted(token_dir.iterdir()):
        if path.suffix != ".json":
            continue
        if not path.name.startswith(_TOKEN_PREFIX):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            scopes = raw.get("scopes") or SCOPES
            creds = Credentials.from_authorized_user_info(raw, scopes=scopes)
            label: str | None = raw.get("account")
            if not label:
                stem = path.stem
                prefix = _TOKEN_PREFIX + "_"
                label = stem[len(prefix):] if stem.startswith(prefix) else stem
            if path.name == _LEGACY_NAME:
                legacy.append((label, creds))
            else:
                labeled.append((label, creds))
        except Exception:  # noqa: BLE001
            log.warning("failed to load token %s", path, exc_info=True)
    for label, creds in legacy + labeled:
        registry[label] = creds
    return registry


# ---------------------------------------------------------------------------
# Per-request credential/service selection (dynamic re-scan, issue #60)
# ---------------------------------------------------------------------------

# No boot-time registry build: we re-scan TOKEN_DIR on every request so a token
# dropped after the server starts is picked up immediately without a restart.
# For a handful of token files the directory scan is negligible.

_token_dir = Path(TOKEN_DIR)

log.info("Gmail MCP ready | TOKEN_DIR=%s", TOKEN_DIR)


def _build_service_registry() -> tuple[dict[str, Any], Any]:
    """Scan TOKEN_DIR and build a label → service dict.

    Returns ``(registry, default_service)`` where ``default_service`` is the
    first entry (the legacy/primary account) or ``None`` when the dir is empty.

    Falls back to the legacy single-file TOKEN_PATH when the directory scan
    yields nothing — preserves backward compat for single-token deploys.
    """
    creds_registry = _load_credentials(_token_dir)

    if not creds_registry:
        # Legacy fallback: try the single TOKEN_PATH.
        try:
            legacy_creds = Credentials.from_authorized_user_file(
                TOKEN_PATH, scopes=SCOPES
            )
            if legacy_creds.expired and legacy_creds.refresh_token:
                legacy_creds.refresh(Request())
            creds_registry["_legacy"] = legacy_creds
        except Exception:  # noqa: BLE001
            pass  # No credentials at all; tools will error on the actual call.

    registry: dict[str, Any] = {}
    for lbl, creds in creds_registry.items():
        try:
            registry[lbl] = build(
                "gmail", "v1", credentials=creds, cache_discovery=False
            )
        except Exception:  # noqa: BLE001
            log.warning("failed to build gmail service for %r", lbl, exc_info=True)

    default = next(iter(registry.values()), None)
    return registry, default


def _get_service_for_label(label: str | None) -> Any:
    """Return the Gmail service for ``label``, re-scanning TOKEN_DIR each call.

    A token dropped after module load is discovered here because we call
    ``_build_service_registry()`` on every invocation.  Falls back to the
    default (first) service when ``label`` is absent or not in the registry.
    """
    registry, default = _build_service_registry()
    if label and label in registry:
        return registry[label]
    return default


def _get_service() -> Any:
    """Return the Gmail service resource for the current request.

    Reads ``X-Account-Label`` from the per-call Starlette request exposed by FastMCP's
    ``request_context`` (set by the MCP lowlevel server *per handler invocation*, not
    per session).  Falls back to the default (first loaded) service when the label is
    absent or unknown.

    Re-scans TOKEN_DIR on every call (issue #60) so tokens dropped at runtime
    are picked up without a restart.

    Why not a ContextVar?  Same reason as mcp-calendar: FastMCP stateful-mode dispatches
    tool handlers in a persistent session task; anyio memory-stream boundaries do not
    propagate contextvars set on later HTTP POSTs.  See calendar/server.py for full
    explanation.
    """
    label: str | None = None
    try:
        req = mcp.get_context().request_context.request
        if req is not None:
            label = req.headers.get(_ACCOUNT_HEADER)
    except LookupError:
        pass
    return _get_service_for_label(label)


# ---------------------------------------------------------------------------
# MIME body walk helper
# ---------------------------------------------------------------------------


def _extract_body(payload: dict[str, Any]) -> str:
    """Walk the MIME part tree and return the best text body.

    Preference order: text/plain > text/html.  Recurses into multipart subtypes.
    Returns an empty string when no text content is found.
    """
    mime = payload.get("mimeType", "")

    # Leaf part: try to decode data
    if mime.startswith("text/"):
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return ""

    # Multipart: recurse into parts
    parts = payload.get("parts") or []
    plain: str | None = None
    html: str | None = None
    for part in parts:
        part_mime = part.get("mimeType", "")
        if part_mime == "text/plain":
            candidate = _extract_body(part)
            if candidate:
                plain = candidate
        elif part_mime == "text/html":
            candidate = _extract_body(part)
            if candidate:
                html = candidate
        elif part_mime.startswith("multipart/"):
            candidate = _extract_body(part)
            if candidate:
                # Treat a plain-text result from a nested multipart as plain
                if plain is None:
                    plain = candidate

    return plain or html or ""


# ---------------------------------------------------------------------------
# RFC822 message build (send + reply)
# ---------------------------------------------------------------------------


def _build_raw_message(
    *,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Build an RFC822 message and return it base64url-encoded for the Gmail API.

    Uses the stdlib :class:`email.message.EmailMessage`. The plain-text ``body`` is the
    primary content; an optional ``html_body`` is added as an alternative part. The
    ``In-Reply-To`` / ``References`` headers thread a reply. The ``From`` header is left
    unset: ``users().messages().send(userId="me")`` stamps the authenticated account, so
    the active-account selection alone decides the sender.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# FastMCP server and tools
# ---------------------------------------------------------------------------

mcp = FastMCP("gmail", host="0.0.0.0", port=PORT)


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


@mcp.tool(name="gmail_list_messages")
async def gmail_list_messages(
    max_results: int = 20,
    page_token: str | None = None,
    label_ids: list[str] | None = None,
) -> str:
    """List recent messages in the inbox (id + threadId). Paginates via page_token."""
    service = _get_service()

    def _call() -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        if label_ids:
            kwargs["labelIds"] = label_ids
        resp = service.users().messages().list(**kwargs).execute()
        return resp.get("messages", [])

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="gmail_get_message")
async def gmail_get_message(message_id: str, format: str = "full") -> str:
    """Fetch a single message by id, with the body extracted from the MIME tree."""
    service = _get_service()

    def _call() -> dict[str, Any]:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format=format)
            .execute()
        )
        # Walk the MIME payload to extract the readable body
        payload = msg.get("payload", {})
        body = _extract_body(payload)
        headers_raw = payload.get("headers", [])
        headers = {h["name"]: h["value"] for h in headers_raw}
        return {
            "id": msg.get("id"),
            "threadId": msg.get("threadId"),
            "labelIds": msg.get("labelIds", []),
            "snippet": msg.get("snippet", ""),
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "date": headers.get("Date", ""),
            "body": body,
        }

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="gmail_search_messages")
async def gmail_search_messages(
    query: str,
    max_results: int = 20,
    page_token: str | None = None,
) -> str:
    """Search messages using Gmail query syntax (e.g. 'from:alice subject:invoice')."""
    service = _get_service()

    def _call() -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": max_results,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        return resp.get("messages", [])

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="gmail_list_drafts")
async def gmail_list_drafts(max_results: int = 20) -> str:
    """List drafts (id + message snippet)."""
    service = _get_service()

    def _call() -> list[dict[str, Any]]:
        resp = (
            service.users()
            .drafts()
            .list(userId="me", maxResults=max_results)
            .execute()
        )
        drafts = resp.get("drafts", [])
        return [
            {"id": d.get("id"), "snippet": d.get("message", {}).get("snippet", "")}
            for d in drafts
        ]

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="gmail_list_labels")
async def gmail_list_labels() -> str:
    """List all labels on the account (id, name, type, messagesTotal)."""
    service = _get_service()

    def _call() -> list[dict[str, Any]]:
        resp = service.users().labels().list(userId="me").execute()
        labels = resp.get("labels", [])
        return [
            {
                "id": lbl.get("id"),
                "name": lbl.get("name"),
                "type": lbl.get("type"),
                "messagesTotal": lbl.get("messagesTotal"),
            }
            for lbl in labels
        ]

    return _json(await asyncio.to_thread(_call))


def _signed_body(
    tool: str, body: str, html_body: str | None
) -> tuple[str, str | None]:
    """Append the transparent assistant signature to an outbound body.

    Delegates to :func:`gmail_signature.inject_signature` (the shared scaffold) so the
    "sent by an assistant" line is added server-side on every send/reply, independent of
    what the model wrote. Returns the ``(body, html_body)`` pair after injection.
    """
    args: dict[str, Any] = {"body": body}
    if html_body is not None:
        args["html_body"] = html_body
    signed = inject_signature(tool, args, SIGNATURE)
    return signed["body"], signed.get("html_body")


@mcp.tool(name="gmail_send_message")
async def gmail_send_message(
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
) -> Any:
    """Send a new message from the active account; the assistant signature is appended.

    Sends on the account selected by ``X-Account-Label`` (falls back to the default).
    Returns the sent message's id + threadId.
    """
    service = _get_service()
    signed_body, signed_html = _signed_body("gmail_send_message", body, html_body)
    raw = _build_raw_message(
        to=to,
        subject=subject,
        body=signed_body,
        cc=cc,
        bcc=bcc,
        html_body=signed_html,
    )

    def _call() -> dict[str, Any]:
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return {"id": sent.get("id"), "threadId": sent.get("threadId")}

    return _json(await asyncio.to_thread(_call))


@mcp.tool(name="gmail_reply_on_message")
async def gmail_reply_on_message(
    message_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
    html_body: str | None = None,
) -> Any:
    """Reply to ``message_id`` on the active account, threaded correctly.

    Fetches the original message to read its ``Message-ID`` / ``References`` / ``Subject``
    / ``From``, then sets ``In-Reply-To`` + ``References`` and the original ``threadId``
    so Gmail threads the reply. The assistant signature is appended server-side.
    """
    service = _get_service()
    signed_body, signed_html = _signed_body("gmail_reply_on_message", body, html_body)

    def _call() -> dict[str, Any]:
        orig = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata")
            .execute()
        )
        thread_id = orig.get("threadId")
        headers = {
            h["name"].lower(): h["value"]
            for h in orig.get("payload", {}).get("headers", [])
        }
        msg_id = headers.get("message-id", "")
        prior_refs = headers.get("references", "")
        references = f"{prior_refs} {msg_id}".strip() if prior_refs else msg_id
        subject = headers.get("subject", "")
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        to = headers.get("reply-to") or headers.get("from", "")
        raw = _build_raw_message(
            to=to,
            subject=reply_subject,
            body=signed_body,
            cc=cc,
            bcc=bcc,
            html_body=signed_html,
            in_reply_to=msg_id,
            references=references,
        )
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw, "threadId": thread_id})
            .execute()
        )
        return {"id": sent.get("id"), "threadId": sent.get("threadId")}

    return _json(await asyncio.to_thread(_call))


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: StarletteRequest) -> JSONResponse:
    """Liveness probe for the compose healthcheck."""
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    import anyio
    import uvicorn

    async def _serve() -> None:
        starlette_app = mcp.streamable_http_app()
        config = uvicorn.Config(
            starlette_app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
        server = uvicorn.Server(config)
        await server.serve()

    anyio.run(_serve)
