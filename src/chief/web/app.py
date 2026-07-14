"""The web UI's ASGI application (#153): routes, auth gate, server-rendered pages.

:func:`build_web_app` assembles a Starlette app from injected collaborators
(:class:`WebDeps`) — the authenticator and, as later slices wire them, the socket
bridge, file areas, settings writers, and health checks. Every page is rendered
server-side (:mod:`chief.web.render`); htmx drives the interactions and SSE the
streaming. All routes except ``/login``, ``/setup``, and ``/static`` sit behind the
session-cookie gate.
"""

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from ..adapters.commands import OWNER_COMMANDS
from ..client_plane import (
    CARD_OPTIONS,
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    TYPE_CARD,
    TYPE_CARD_RESOLVED,
    TYPE_DELTA,
    TYPE_ERROR,
    TYPE_FILE,
    TYPE_MILESTONE,
    TYPE_REPLY,
    TYPE_TOOL,
)
from ..config import parse_hhmm
from ..persistence import imessage as imessage_repo
from ..persistence import watches as watches_repo
from ..persistence.watches import effective_state as watches_effective_state
from . import render
from .auth import MIN_PASSWORD_LENGTH, SESSION_COOKIE, SESSION_MAX_AGE, WebAuth
from .bridge import BridgeError, SocketBridge
from .files import (
    UPLOAD_AREA,
    FileAreas,
    ForbiddenPathError,
    UnknownAreaError,
)
from .health import HealthCheck
from .settings_io import PLATFORM_FIELDS, SettingsPanel

_STATIC_DIR = Path(__file__).parent / "static"

#: Paths reachable without a session: the two credential pages and the assets they
#: need. Everything else redirects to login (or setup, before a password exists).
_OPEN_PATHS = ("/login", "/setup")
_OPEN_PREFIXES = ("/static/",)


@dataclass
class IMessagePanel:
    """The web side of the iMessage whitelist (#156): DB-backed, unlike the
    file-backed curated settings — edits apply immediately, no restart."""

    session_factory: async_sessionmaker[AsyncSession]

    async def entries(self) -> list[tuple[str, str, str]]:
        """Whitelist rows as ``(handle, tier, mode)`` for the renderer."""
        async with self.session_factory() as session:
            rows = await imessage_repo.list_whitelist(session)
        return [
            (
                contact.user_id,
                contact.tier,
                pref.mode if pref is not None else imessage_repo.MODE_AUTO,
            )
            for contact, pref in rows
        ]

    async def unknown(self) -> list[tuple[str, int, str]]:
        """Unknown-sender rows as ``(handle, count, last_seen)`` — metadata only."""
        async with self.session_factory() as session:
            rows = await imessage_repo.list_unknown_senders(
                session, platform=imessage_repo.PLATFORM
            )
        return [
            (row.handle, row.count, f"{row.last_seen:%Y-%m-%d %H:%M} UTC")
            for row in rows
        ]

    async def add(self, handle: str, mode: str) -> None:
        async with self.session_factory() as session:
            await imessage_repo.add_handle(
                session,
                handle=handle,
                tier=imessage_repo.TIER_GUEST,
                mode=mode,
            )

    async def remove(self, handle: str) -> bool:
        async with self.session_factory() as session:
            return await imessage_repo.remove_handle(session, handle)

    async def set_mode(self, handle: str, mode: str) -> bool:
        async with self.session_factory() as session:
            return await imessage_repo.set_mode(session, handle, mode) is not None


@dataclass
class WatchesPanel:
    """The web side of watches (#165): read-only — manage via chat or /watches."""

    session_factory: async_sessionmaker[AsyncSession]

    async def entries(self) -> list[tuple[str, str, str, str, str]]:
        """``(target, instruction, expiry, tone, state)`` rows, newest first."""
        async with self.session_factory() as session:
            rows = await watches_repo.list_watches(session)
        now = datetime.now(UTC)
        return [
            (
                w.target_handle,
                w.instruction,
                f"{w.expiry:%Y-%m-%d %H:%M} UTC",
                w.tone,
                watches_effective_state(w, now=now),
            )
            for w in rows
        ]


@dataclass
class WebDeps:
    """Everything the app serves with — injected, never constructed by routes."""

    auth: WebAuth
    #: The client-plane attachment. ``None`` only when the socket is unreachable —
    #: the chat surface then renders a "not connected" note instead of crashing.
    bridge: SocketBridge | None = None
    #: The exposed file roots (workspace + screenshots). ``None`` hides the surface.
    files: FileAreas | None = None
    #: The curated settings write targets. ``None`` hides the settings forms.
    settings_panel: SettingsPanel | None = None
    #: The iMessage whitelist panel (#156). ``None`` hides that section.
    imessage: IMessagePanel | None = None
    #: The watches read-only panel (#165). ``None`` hides that section.
    watches: WatchesPanel | None = None
    #: The health page's pluggable checklist (append to extend the page).
    health: tuple[HealthCheck, ...] | list[HealthCheck] = ()


def _session_token(scope: Scope) -> str | None:
    """Pull the session cookie's value out of a raw ASGI scope, or ``None``."""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(value.decode("latin-1"))
            morsel = cookie.get(SESSION_COOKIE)
            if morsel is not None:
                return morsel.value
    return None


class AuthGate:
    """Pure-ASGI middleware: every non-open path requires a live session.

    Kept transport-level (no starlette middleware base class) so the gate cannot be
    bypassed by a route wiring mistake — it wraps the whole app, static mounts and
    all, and only the explicit allowlist passes through unauthenticated.
    """

    def __init__(self, app: ASGIApp, auth: WebAuth) -> None:
        self._app = app
        self._auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"]
        if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
            await self._app(scope, receive, send)
            return
        if self._auth.session_valid(_session_token(scope)):
            await self._app(scope, receive, send)
            return
        target = "/login" if self._auth.password_set else "/setup"
        response = RedirectResponse(target, status_code=303)
        await response(scope, receive, send)


def _set_session_cookie(response: Response, token: str) -> None:
    # No ``secure`` flag: v1 is plain HTTP on a home LAN by design (the PRD's
    # threat model); TLS termination is reverse-proxy guidance, not code.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


async def _form_str(request: Request, field: str) -> str:
    value = (await request.form()).get(field)
    return value if isinstance(value, str) else ""


def _active_thread(request: Request) -> tuple[str, str]:
    """The (platform, thread_key) the page/stream is focused on — client state."""
    platform = request.query_params.get("platform") or CLI_PLATFORM
    thread_key = request.query_params.get("thread_key") or DEFAULT_THREAD_KEY
    return platform, thread_key


def _sse_format(event: str, data: str) -> str:
    """One SSE event: named, with multi-line data split across ``data:`` lines."""
    payload = "".join(f"data: {line}\n" for line in (data.splitlines() or [""]))
    return f"event: {event}\n{payload}\n"


def _sse_events_for(
    frame: dict[str, object],
    *,
    platform: str,
    thread_key: str,
    bridge: SocketBridge,
) -> list[tuple[str, str]]:
    """Translate one broadcast frame into named SSE events for one open page.

    The active thread is client state (#134): replies/milestones/files render only
    when they match it, while approval traffic re-renders the approvals block for
    EVERY page — a card raised on any platform is answerable from any browser tab.
    """
    ftype = frame.get("type")
    matches = (
        frame.get("platform") == platform and frame.get("thread_key") == thread_key
    )
    events: list[tuple[str, str]] = []
    if ftype == TYPE_REPLY and matches:
        events.append(
            ("message", render.message_html("reply", str(frame.get("text"))))
        )
    elif ftype == TYPE_MILESTONE and matches:
        events.append(
            ("message", render.message_html("milestone", str(frame.get("text"))))
        )
    elif ftype == TYPE_FILE and matches:
        caption = frame.get("caption")
        events.append(
            (
                "message",
                render.file_message_html(
                    platform,
                    thread_key,
                    str(frame.get("filename")),
                    str(caption) if caption is not None else None,
                ),
            )
        )
    elif ftype == TYPE_DELTA and matches:
        # JSON, not HTML: the client accumulates increments into a streaming
        # bubble itself (textContent assignment escapes, so no markup needed).
        events.append(
            (
                "delta",
                json.dumps(
                    {
                        "message_id": frame.get("message_id"),
                        "text": frame.get("text"),
                        "done": bool(frame.get("done")),
                    }
                ),
            )
        )
    elif ftype == TYPE_TOOL and matches:
        events.append(
            (
                "tool",
                json.dumps(
                    {
                        "tool_call_id": frame.get("tool_call_id"),
                        "name": frame.get("name"),
                        "status": frame.get("status"),
                        "ok": frame.get("ok"),
                        "detail": frame.get("detail"),
                    }
                ),
            )
        )
    elif ftype in (TYPE_CARD, TYPE_CARD_RESOLVED):
        events.append(
            (
                "approvals",
                render.approvals_html(
                    bridge.pending_cards(),
                    active_platform=platform,
                    active_thread=thread_key,
                ),
            )
        )
        if ftype == TYPE_CARD_RESOLVED and matches:
            events.append(
                ("message", render.message_html("milestone", str(frame.get("text"))))
            )
    elif ftype == TYPE_ERROR:
        # Connection-scoped (a failed command, a refused answer) — single-owner
        # surface, so a dim transcript notice on every open page is honest enough.
        events.append(
            ("message", render.message_html("milestone", f"⚠ {frame.get('message')}"))
        )
    return events


def build_web_app(deps: WebDeps) -> Starlette:
    """Assemble the ASGI app over ``deps``; pure construction, no I/O."""

    async def index(request: Request) -> Response:
        return RedirectResponse("/chat", status_code=303)

    async def login_form(request: Request) -> Response:
        if not deps.auth.password_set:
            return RedirectResponse("/setup", status_code=303)
        return HTMLResponse(render.login_page())

    async def login_submit(request: Request) -> Response:
        password = await _form_str(request, "password")
        if not deps.auth.verify(password):
            return HTMLResponse(
                render.login_page("Wrong password."), status_code=401
            )
        response = RedirectResponse("/chat", status_code=303)
        _set_session_cookie(response, deps.auth.issue_session())
        return response

    async def setup_form(request: Request) -> Response:
        if deps.auth.password_set:
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(render.setup_page())

    async def setup_submit(request: Request) -> Response:
        if deps.auth.password_set:
            # First write wins; a later POST (another device, a replay) can never
            # overwrite the credential from outside a session.
            return HTMLResponse(
                render.login_page("A password is already set."), status_code=409
            )
        password = await _form_str(request, "password")
        confirm = await _form_str(request, "confirm")
        if len(password) < MIN_PASSWORD_LENGTH:
            return HTMLResponse(
                render.setup_page(
                    f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
                ),
                status_code=400,
            )
        if password != confirm:
            return HTMLResponse(
                render.setup_page("Passwords do not match."), status_code=400
            )
        deps.auth.set_password(password)
        response = RedirectResponse("/chat", status_code=303)
        _set_session_cookie(response, deps.auth.issue_session())
        return response

    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            deps.auth.revoke(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def chat_page(request: Request) -> Response:
        if deps.bridge is None:
            body = '<h1>Chat</h1><p class="note">Chat plane not connected.</p>'
            return HTMLResponse(render.page("Chat", body, active="/chat"))
        platform, thread_key = _active_thread(request)
        threads = await deps.bridge.threads()
        try:
            history = await deps.bridge.backfill(platform, thread_key)
        except BridgeError:
            # A thread with no task row yet (day one, or a freshly minted key)
            # simply has no history — the composer still dispatches into it.
            history = []
        body = render.chat_page_body(
            platform=platform,
            thread_key=thread_key,
            threads=threads,
            history=history,
            cards=deps.bridge.pending_cards(),
            commands=OWNER_COMMANDS.entries(),
        )
        return HTMLResponse(render.page("Chat", body, active="/chat"))

    async def chat_threads(request: Request) -> Response:
        if deps.bridge is None:
            return HTMLResponse("")
        platform, thread_key = _active_thread(request)
        return HTMLResponse(
            render.thread_list_html(
                await deps.bridge.threads(),
                active_platform=platform,
                active_thread=thread_key,
            )
        )

    async def chat_send(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        form = await request.form()
        platform = str(form.get("platform") or CLI_PLATFORM)
        thread_key = str(form.get("thread_key") or DEFAULT_THREAD_KEY)
        text = form.get("text")
        if not isinstance(text, str) or not text.strip():
            return PlainTextResponse("text is required", status_code=400)
        text = text.strip()
        if platform == CLI_PLATFORM and text.startswith("/"):
            name, _, arg = text[1:].partition(" ")
            await deps.bridge.send_command(thread_key, name, arg.strip())
        elif platform == CLI_PLATFORM:
            await deps.bridge.send_user(thread_key, text)
        else:
            # A foreign platform's thread: a real owner turn on that platform's own
            # engine (#135) — echoed onto the phone chat, replied via its mirror.
            await deps.bridge.send_inject(platform, thread_key, text)
        # The socket does not echo inbound frames back, so the POST response IS the
        # owner line — htmx appends it to the transcript.
        return HTMLResponse(render.message_html("user", text, role="owner"))

    async def chat_cancel(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        form = await request.form()
        platform = str(form.get("platform") or CLI_PLATFORM)
        thread_key = str(form.get("thread_key") or DEFAULT_THREAD_KEY)
        if platform != CLI_PLATFORM:
            return PlainTextResponse(
                "cancel is only available on chief's own threads", status_code=400
            )
        await deps.bridge.send_command(thread_key, "cancel")
        return PlainTextResponse("")

    async def chat_new(request: Request) -> Response:
        # Mint a fresh client-side thread key; the task row appears on first send.
        thread_key = f"cli:w{time.time_ns()}"
        return RedirectResponse(
            f"/chat?platform={CLI_PLATFORM}&thread_key={thread_key}",
            status_code=303,
        )

    async def files_page(request: Request) -> Response:
        if deps.files is None:
            body = '<h1>Files</h1><p class="note">File access is not configured.</p>'
            return HTMLResponse(render.page("Files", body, active="/files"))
        areas = deps.files.names()
        area = request.query_params.get("area") or (areas[0] if areas else "")
        try:
            entries = deps.files.list(area)
        except UnknownAreaError:
            return PlainTextResponse("no such file area", status_code=404)
        body = render.files_page_body(
            areas=areas,
            active=area,
            entries=entries,
            uploads_enabled=UPLOAD_AREA in areas,
        )
        return HTMLResponse(render.page("Files", body, active="/files"))

    async def files_upload(request: Request) -> Response:
        if deps.files is None:
            return PlainTextResponse("file access not configured", status_code=503)
        # The context manager closes the spooled temp file the parser created —
        # without it the upload's buffer lingers until GC.
        async with request.form() as form:
            area = str(form.get("area") or UPLOAD_AREA)
            if area != UPLOAD_AREA:
                # Uploads land in the agent workspace ONLY — screenshots stay
                # read-only.
                return PlainTextResponse(
                    "uploads go to the workspace only", status_code=400
                )
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                return PlainTextResponse("a file is required", status_code=400)
            try:
                deps.files.save_upload(
                    upload.filename or "upload", await upload.read()
                )
            except UnknownAreaError:
                return PlainTextResponse(
                    "workspace is not configured", status_code=400
                )
        return RedirectResponse(f"/files?area={UPLOAD_AREA}", status_code=303)

    async def files_download(request: Request) -> Response:
        if deps.files is None:
            return PlainTextResponse("file access not configured", status_code=503)
        area = request.query_params.get("area") or ""
        relative = request.query_params.get("path") or ""
        try:
            path = deps.files.open_path(area, relative)
        except (UnknownAreaError, ForbiddenPathError):
            # One opaque 404 for every miss: an outside probe learns nothing about
            # which paths exist beyond the fence.
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(
            path, filename=path.name, content_disposition_type="attachment"
        )

    async def approvals_partial(request: Request) -> Response:
        if deps.bridge is None:
            return HTMLResponse("")
        platform, thread_key = _active_thread(request)
        return HTMLResponse(
            render.approvals_html(
                deps.bridge.pending_cards(),
                active_platform=platform,
                active_thread=thread_key,
            )
        )

    async def approvals_answer(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        approval_id = int(request.path_params["approval_id"])
        form = await request.form()
        action = form.get("action")
        action = action if isinstance(action, str) else ""
        # The page's active thread, carried by the card's hx-vals, so the
        # optimistic re-render below stays scoped the way the page renders.
        platform = str(form.get("platform") or CLI_PLATFORM)
        thread_key = str(form.get("thread_key") or DEFAULT_THREAD_KEY)
        if action not in {option["action"] for option in CARD_OPTIONS}:
            return PlainTextResponse(f"unknown action {action!r}", status_code=400)
        if approval_id not in deps.bridge.cards:
            # The bridge's card ledger is authoritative: a resolved (or never-seen)
            # id means someone already decided — first answer wins, everywhere.
            return PlainTextResponse(
                f"approval {approval_id} is unknown or already resolved",
                status_code=409,
            )
        await deps.bridge.answer(approval_id, action)
        # Optimistic render: drop the just-answered card now; the authoritative
        # card_resolved broadcast re-renders the block for every other page.
        remaining = [
            card
            for card in deps.bridge.pending_cards()
            if card.get("approval_id") != approval_id
        ]
        return HTMLResponse(
            render.approvals_html(
                remaining, active_platform=platform, active_thread=thread_key
            )
        )

    async def _settings_page_html(error: str | None = None) -> str:
        panel = deps.settings_panel
        assert panel is not None
        secrets = panel.secrets
        # Display the freshest written values (the config file), falling back to
        # the boot snapshot for keys the owner never edited.
        stored = panel.config.read()

        def current(key: str) -> object:
            return stored.get(key, getattr(panel.settings, key))

        imessage_html = ""
        if deps.imessage is not None:
            imessage_html = render.imessage_section(
                await deps.imessage.entries(), await deps.imessage.unknown()
            )
        watches_html = ""
        if deps.watches is not None:
            watches_html = render.watches_section(await deps.watches.entries())
        body = render.settings_page_body(
            telegram_connected=secrets.exists("telegram_bot_token"),
            discord_connected=secrets.exists("discord_bot_token"),
            openrouter_connected=secrets.exists("openrouter_api_key"),
            current={
                key: current(key)
                for key in (
                    "owner_model_default",
                    "web_lan_enabled",
                    "quiet_hours_start",
                    "quiet_hours_end",
                )
            },
            imessage_html=imessage_html,
            watches_html=watches_html,
            error=error,
        )
        return render.page("Settings", body, active="/settings")

    async def settings_page(request: Request) -> Response:
        if deps.settings_panel is None:
            body = '<h1>Settings</h1><p class="note">Settings are not wired.</p>'
            return HTMLResponse(render.page("Settings", body, active="/settings"))
        return HTMLResponse(await _settings_page_html())

    async def settings_imessage(request: Request) -> Response:
        """Whitelist edits (#156): add / remove / flip delegation mode.

        DB-backed and effective immediately — no restart banner applies here.
        """
        if deps.imessage is None:
            return PlainTextResponse("imessage is not enabled", status_code=404)
        form = await request.form()
        action = str(form.get("action") or "")
        handle = str(form.get("handle") or "").strip()
        if not handle:
            return HTMLResponse(
                await _settings_page_html("A handle is required."),
                status_code=400,
            )
        if action == "add":
            mode = (
                imessage_repo.MODE_DRAFT
                if form.get("draft") is not None
                or form.get("mode") == imessage_repo.MODE_DRAFT
                else imessage_repo.MODE_AUTO
            )
            await deps.imessage.add(handle, mode)
        elif action == "remove":
            await deps.imessage.remove(handle)
        elif action == "mode":
            mode = str(form.get("mode") or "")
            if mode not in (imessage_repo.MODE_AUTO, imessage_repo.MODE_DRAFT):
                return HTMLResponse(
                    await _settings_page_html("Mode must be auto or draft."),
                    status_code=400,
                )
            if not await deps.imessage.set_mode(handle, mode):
                return HTMLResponse(
                    await _settings_page_html(
                        f"{handle} is not on the whitelist."
                    ),
                    status_code=400,
                )
        else:
            return HTMLResponse(
                await _settings_page_html(f"Unknown action {action!r}."),
                status_code=400,
            )
        return RedirectResponse("/settings", status_code=303)

    async def settings_platform(request: Request) -> Response:
        if deps.settings_panel is None:
            return PlainTextResponse("settings not wired", status_code=503)
        panel = deps.settings_panel
        platform = request.path_params["platform"]
        fields = PLATFORM_FIELDS.get(platform)
        if fields is None:
            return PlainTextResponse("no such platform", status_code=404)
        token_file, owner_id_key = fields
        form = await request.form()
        if form.get("action") == "disconnect":
            panel.secrets.delete(token_file)
            # 0 is the committed "unset" sentinel for owner ids (config.yaml).
            panel.config.update({owner_id_key: 0})
            return RedirectResponse("/settings", status_code=303)
        token = str(form.get("token") or "").strip()
        raw_owner_id = str(form.get("owner_id") or "").strip()
        if not token:
            return HTMLResponse(
                await _settings_page_html("A bot token is required."), status_code=400
            )
        if not raw_owner_id.isdigit() or int(raw_owner_id) <= 0:
            return HTMLResponse(
                await _settings_page_html("The owner id must be a positive number."),
                status_code=400,
            )
        error = await panel.validators[platform](token)
        if error is not None:
            return HTMLResponse(await _settings_page_html(error), status_code=400)
        panel.secrets.write(token_file, token)
        panel.config.update({owner_id_key: int(raw_owner_id)})
        return RedirectResponse("/settings", status_code=303)

    async def settings_openrouter(request: Request) -> Response:
        if deps.settings_panel is None:
            return PlainTextResponse("settings not wired", status_code=503)
        panel = deps.settings_panel
        form = await request.form()
        if form.get("action") == "disconnect":
            panel.secrets.delete("openrouter_api_key")
            return RedirectResponse("/settings", status_code=303)
        key = str(form.get("api_key") or "").strip()
        if not key:
            return HTMLResponse(
                await _settings_page_html("An API key is required."), status_code=400
            )
        error = await panel.validators["openrouter"](key)
        if error is not None:
            return HTMLResponse(await _settings_page_html(error), status_code=400)
        panel.secrets.write("openrouter_api_key", key)
        return RedirectResponse("/settings", status_code=303)

    async def settings_model(request: Request) -> Response:
        if deps.settings_panel is None:
            return PlainTextResponse("settings not wired", status_code=503)
        model = (await _form_str(request, "owner_model_default")).strip()
        if not model:
            return HTMLResponse(
                await _settings_page_html("A model id is required."), status_code=400
            )
        deps.settings_panel.config.update({"owner_model_default": model})
        return RedirectResponse("/settings", status_code=303)

    async def settings_web(request: Request) -> Response:
        if deps.settings_panel is None:
            return PlainTextResponse("settings not wired", status_code=503)
        form = await request.form()
        lan = form.get("lan") is not None  # unchecked checkboxes are absent
        deps.settings_panel.config.update({"web_lan_enabled": lan})
        return RedirectResponse("/settings", status_code=303)

    async def settings_quiet_hours(request: Request) -> Response:
        if deps.settings_panel is None:
            return PlainTextResponse("settings not wired", status_code=503)
        form = await request.form()
        start = str(form.get("start") or "").strip()
        end = str(form.get("end") or "").strip()
        if start and parse_hhmm(start) is None:
            return HTMLResponse(
                await _settings_page_html("Quiet-hours start must be 24-hour HH:MM."),
                status_code=400,
            )
        if not end or parse_hhmm(end) is None:
            return HTMLResponse(
                await _settings_page_html("Quiet-hours end must be 24-hour HH:MM."),
                status_code=400,
            )
        deps.settings_panel.config.update(
            {"quiet_hours_start": start or None, "quiet_hours_end": end}
        )
        return RedirectResponse("/settings", status_code=303)

    async def settings_password(request: Request) -> Response:
        form = await request.form()
        current = str(form.get("current") or "")
        password = str(form.get("password") or "")
        confirm = str(form.get("confirm") or "")
        error: str | None = None
        if not deps.auth.verify(current):
            error = "The current password is wrong."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = (
                f"The new password needs at least {MIN_PASSWORD_LENGTH} characters."
            )
        elif password != confirm:
            error = "The new passwords do not match."
        if error is not None:
            if deps.settings_panel is not None:
                return HTMLResponse(await _settings_page_html(error), status_code=400)
            return PlainTextResponse(error, status_code=400)
        deps.auth.set_password(password)  # revokes every session, everywhere
        response = RedirectResponse("/settings", status_code=303)
        # Keep THIS browser logged in: mint a fresh session under the new
        # credential — every other cookie in the world is now dead.
        _set_session_cookie(response, deps.auth.issue_session())
        return response

    async def health_page(request: Request) -> Response:
        items = [await check() for check in deps.health]
        return HTMLResponse(
            render.page("Health", render.health_page_body(items), active="/health")
        )

    async def events(request: Request) -> Response:
        if deps.bridge is None:
            return PlainTextResponse("chat plane not connected", status_code=503)
        bridge = deps.bridge
        platform, thread_key = _active_thread(request)
        subscription = bridge.subscribe()

        async def stream() -> AsyncIterator[str]:
            with subscription:
                yield "retry: 2000\n\n"
                while True:
                    frame = await subscription.queue.get()
                    for name, fragment in _sse_events_for(
                        frame,
                        platform=platform,
                        thread_key=thread_key,
                        bridge=bridge,
                    ):
                        yield _sse_format(name, fragment)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    routes = [
        Route("/", index),
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/setup", setup_form, methods=["GET"]),
        Route("/setup", setup_submit, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/chat", chat_page, methods=["GET"]),
        Route("/chat/threads", chat_threads, methods=["GET"]),
        Route("/chat/send", chat_send, methods=["POST"]),
        Route("/chat/cancel", chat_cancel, methods=["POST"]),
        Route("/chat/new", chat_new, methods=["GET"]),
        Route("/files", files_page, methods=["GET"]),
        Route("/files/upload", files_upload, methods=["POST"]),
        Route("/files/download", files_download, methods=["GET"]),
        Route("/approvals", approvals_partial, methods=["GET"]),
        Route("/approvals/{approval_id:int}", approvals_answer, methods=["POST"]),
        Route("/settings", settings_page, methods=["GET"]),
        Route("/settings/imessage", settings_imessage, methods=["POST"]),
        Route("/settings/platform/{platform}", settings_platform, methods=["POST"]),
        Route("/settings/openrouter", settings_openrouter, methods=["POST"]),
        Route("/settings/model", settings_model, methods=["POST"]),
        Route("/settings/web", settings_web, methods=["POST"]),
        Route("/settings/quiet-hours", settings_quiet_hours, methods=["POST"]),
        Route("/settings/password", settings_password, methods=["POST"]),
        Route("/health", health_page, methods=["GET"]),
        Route("/events", events, methods=["GET"]),
        Mount("/static", StaticFiles(directory=_STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(AuthGate, auth=deps.auth)
    return app
