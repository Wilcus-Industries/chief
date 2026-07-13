"""Server-rendered HTML for the web UI (#153). No template engine, no Node.

Small pure functions building escaped HTML strings — the whole view layer. Pages share
one :func:`page` layout (phone-first, single inline stylesheet, htmx + its SSE
extension served from ``/static``); interactive routes return *fragments* the client
swaps in via htmx or SSE. Every interpolated value passes :func:`html.escape`.
"""

import html
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Protocol
from urllib.parse import urlencode


class FileRow(Protocol):
    """What :func:`files_page_body` needs from a listed file (read-only)."""

    @property
    def path(self) -> str: ...
    @property
    def size(self) -> int: ...
    @property
    def modified(self) -> float: ...

#: Nav entries: (href, label). The active one is highlighted by :func:`page`.
_NAV: tuple[tuple[str, str], ...] = (
    ("/chat", "Chat"),
    ("/files", "Files"),
    ("/settings", "Settings"),
    ("/health", "Health"),
)

_STYLE = """
:root { color-scheme: light dark;
  --bg: #f6f6f4; --fg: #1c1c1a; --muted: #6f6f68; --line: #d9d9d2;
  --panel: #ffffff; --accent: #2f6f4f; --accent-fg: #ffffff; --danger: #a03030; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161614; --fg: #e8e8e2; --muted: #93938a; --line: #33332e;
    --panel: #1f1f1c; --accent: #4d9973; --accent-fg: #10130f; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { display: flex; align-items: baseline; gap: 1rem; padding: .6rem .9rem;
  border-bottom: 1px solid var(--line); position: sticky; top: 0;
  background: var(--bg); z-index: 5; flex-wrap: wrap; }
header .brand { font-weight: 700; letter-spacing: .04em; }
nav { display: flex; gap: .8rem; }
nav a { color: var(--muted); text-decoration: none; padding: .1rem 0; }
nav a.active { color: var(--fg); border-bottom: 2px solid var(--accent); }
main { max-width: 44rem; margin: 0 auto; padding: .9rem; }
h1 { font-size: 1.15rem; margin: .2rem 0 .8rem; }
h2 { font-size: 1rem; margin: 1rem 0 .4rem; }
form.stack { display: flex; flex-direction: column; gap: .55rem; }
input, select, button, textarea { font: inherit; color: inherit; }
input[type=text], input[type=password], input[type=file], select, textarea {
  width: 100%; padding: .5rem .6rem; border: 1px solid var(--line);
  border-radius: .45rem; background: var(--panel); }
button { padding: .5rem .9rem; border: 0; border-radius: .45rem;
  background: var(--accent); color: var(--accent-fg); cursor: pointer; }
button.quiet { background: transparent; color: var(--muted);
  border: 1px solid var(--line); }
button.danger { background: var(--danger); color: #fff; }
.error { color: var(--danger); margin: .4rem 0; }
.note { color: var(--muted); font-size: .88rem; }
.panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: .6rem; padding: .7rem .8rem; margin: .5rem 0; }
/* chat */
.chat { display: flex; flex-direction: column;
  min-height: calc(100dvh - 8.5rem); }
.threads { display: flex; gap: .4rem; overflow-x: auto; padding-bottom: .4rem; }
.threads a { white-space: nowrap; text-decoration: none; color: var(--muted);
  border: 1px solid var(--line); border-radius: 1rem; padding: .15rem .7rem;
  font-size: .88rem; }
.threads a.active { color: var(--fg); border-color: var(--accent); }
#transcript { flex: 1; overflow-y: auto; display: flex;
  flex-direction: column; gap: .45rem; padding: .4rem 0; }
.msg { padding: .45rem .65rem; border-radius: .55rem; max-width: 92%;
  overflow-wrap: break-word; white-space: pre-wrap; }
.msg.owner { align-self: flex-end; background: var(--accent);
  color: var(--accent-fg); }
.msg.chief { align-self: flex-start; background: var(--panel);
  border: 1px solid var(--line); }
.msg.milestone { align-self: flex-start; color: var(--muted);
  font-size: .88rem; padding: 0 .65rem; }
.msg.file a { color: inherit; }
.card { border: 1px solid var(--accent); border-radius: .6rem;
  padding: .6rem .8rem; margin: .4rem 0; background: var(--panel); }
.card .actions { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem; }
.card button { font-size: .88rem; padding: .35rem .6rem; }
.composer { display: flex; gap: .5rem; position: sticky; bottom: 0;
  background: var(--bg); padding: .5rem 0; }
.composer input { flex: 1; }
/* tables */
table { width: 100%; border-collapse: collapse; }
td, th { text-align: left; padding: .35rem .3rem;
  border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: .88rem; }
.ok { color: var(--accent); } .bad { color: var(--danger); }
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str, *, active: str | None = None) -> str:
    """The shared layout: header nav + one ``main`` column, phone-first."""
    nav = "".join(
        f'<a href="{href}"{" class=\"active\"" if href == active else ""}>'
        f"{label}</a>"
        for href, label in _NAV
    )
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)} — chief</title>"
        f"<style>{_STYLE}</style>"
        '<script src="/static/htmx.min.js"></script>'
        '<script src="/static/htmx-sse.js"></script>'
        "</head><body>"
        f'<header><span class="brand">chief</span><nav>{nav}</nav></header>'
        f"<main>{body}</main>"
        "</body></html>"
    )


def bare_page(title: str, body: str) -> str:
    """A layout without the nav — for login/setup, where nothing else is reachable."""
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)} — chief</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        '<header><span class="brand">chief</span></header>'
        f"<main>{body}</main>"
        "</body></html>"
    )


def login_page(error: str | None = None) -> str:
    body = (
        "<h1>Log in</h1>"
        + (f'<p class="error">{_esc(error)}</p>' if error else "")
        + '<form class="stack" method="post" action="/login">'
        '<input type="password" name="password" placeholder="Password"'
        " autofocus autocomplete=\"current-password\">"
        "<button>Log in</button></form>"
    )
    return bare_page("Log in", body)


def setup_page(error: str | None = None) -> str:
    body = (
        "<h1>Welcome — set your password</h1>"
        '<p class="note">This is the one credential for this chief. It is stored'
        " hashed in the secrets directory; you stay logged in on this browser.</p>"
        + (f'<p class="error">{_esc(error)}</p>' if error else "")
        + '<form class="stack" method="post" action="/setup">'
        '<input type="password" name="password" placeholder="New password'
        ' (min 8 chars)" autofocus autocomplete="new-password">'
        '<input type="password" name="confirm" placeholder="Confirm password"'
        ' autocomplete="new-password">'
        "<button>Set password &amp; start</button></form>"
    )
    return bare_page("Set up", body)


def message_html(kind: str, text: str, *, role: str = "chief") -> str:
    """One transcript entry: an owner line, a chief reply, or a dim milestone."""
    if kind == "milestone":
        return f'<div class="msg milestone">· {_esc(text)}</div>'
    css = "owner" if role == "owner" else "chief"
    return f'<div class="msg {css}">{_esc(text)}</div>'


def file_message_html(
    platform: str, thread_key: str, filename: str, caption: str | None
) -> str:
    """A delivered-file transcript entry linking to the workspace download."""
    label = _esc(caption or filename)
    return (
        f'<div class="msg chief file">📄 {_esc(filename)}'
        f'<div class="note">{label}</div></div>'
    )


def card_html(card: Mapping[str, object]) -> str:
    """An actionable approval card: preview text + the four answer buttons."""
    approval_id = _esc(card.get("approval_id"))
    options = card.get("options")
    buttons = "".join(
        f'<button hx-post="/approvals/{approval_id}"'
        f' hx-vals=\'{{"action": "{_esc(o["action"])}"}}\''
        f' hx-target="#approvals" hx-swap="innerHTML">{_esc(o["label"])}</button>'
        for o in options
        if isinstance(o, dict)
    ) if isinstance(options, list) else ""
    origin = f'{_esc(card.get("platform"))} · {_esc(card.get("thread_key"))}'
    return (
        f'<div class="card" id="card-{approval_id}">'
        f'<div class="note">{origin}</div>'
        f"<div>{_esc(card.get('text'))}</div>"
        f'<div class="actions">{buttons}</div></div>'
    )


def approvals_html(cards: Iterable[Mapping[str, object]]) -> str:
    """The pending-approvals block (swapped whole on every card event)."""
    rendered = "".join(card_html(c) for c in cards)
    return rendered or ""


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover — unreachable


def files_page_body(
    *,
    areas: Iterable[str],
    active: str,
    entries: Iterable[FileRow],
    uploads_enabled: bool,
) -> str:
    """The files surface: area tabs, an upload form, and the listing table."""
    tabs = "".join(
        f'<a href="/files?area={_esc(a)}"'
        f'{" class=\"active\"" if a == active else ""}>{_esc(a)}</a>'
        for a in areas
    )
    upload = (
        '<form class="stack" method="post" action="/files/upload"'
        ' enctype="multipart/form-data">'
        '<input type="file" name="file" required>'
        "<button>Upload to workspace</button></form>"
        if uploads_enabled and active == "workspace"
        else ""
    )
    rows = "".join(
        f'<tr><td><a href="/files/download?'
        f"{_esc(urlencode({'area': active, 'path': e.path}))}\">"
        f"{_esc(e.path)}</a></td>"
        f"<td>{_human_size(e.size)}</td>"
        f"<td>{datetime.fromtimestamp(e.modified).strftime('%Y-%m-%d %H:%M')}</td>"
        "</tr>"
        for e in entries
    )
    table = (
        "<table><tr><th>File</th><th>Size</th><th>Modified</th></tr>"
        f"{rows}</table>"
        if rows
        else '<p class="note">No files here yet.</p>'
    )
    return (
        "<h1>Files</h1>"
        f'<div class="threads">{tabs}</div>'
        f"{upload}{table}"
    )


class HealthRow(Protocol):
    """What :func:`health_page_body` needs from one checklist item."""

    @property
    def name(self) -> str: ...
    @property
    def ok(self) -> bool | None: ...
    @property
    def detail(self) -> str: ...


def health_page_body(items: Iterable[HealthRow]) -> str:
    """The pluggable checklist, one row per item: verdict mark, name, detail."""
    rows = "".join(
        "<tr>"
        f"<td>{_mark(item.ok)}</td>"
        f"<td>{_esc(item.name)}</td>"
        f'<td class="note">{_esc(item.detail)}</td>'
        "</tr>"
        for item in items
    )
    return f"<h1>Health</h1><table>{rows}</table>"


def _mark(ok: bool | None) -> str:
    if ok is None:
        return "·"
    return '<span class="ok">✔</span>' if ok else '<span class="bad">✘</span>'


def _platform_section(name: str, key: str, connected: bool) -> str:
    if connected:
        return (
            f'<div class="panel"><h2>{_esc(name)}</h2>'
            '<p><span class="ok">✔ Connected</span>'
            ' <span class="note">(restart applies changes)</span></p>'
            f'<form method="post" action="/settings/platform/{key}">'
            '<input type="hidden" name="action" value="disconnect">'
            '<button class="danger">Disconnect</button></form></div>'
        )
    return (
        f'<div class="panel"><h2>{_esc(name)}</h2>'
        '<p class="note">Not connected.</p>'
        f'<form class="stack" method="post" action="/settings/platform/{key}">'
        f'<input type="text" name="token" placeholder="{_esc(name)} bot token">'
        '<input type="text" name="owner_id" placeholder="Your numeric user id"'
        ' inputmode="numeric">'
        "<button>Validate &amp; connect</button></form></div>"
    )


def imessage_section(
    entries: Iterable[tuple[str, str, str]],
    unknown: Iterable[tuple[str, int, str]],
) -> str:
    """The iMessage whitelist panel (#156): handles, modes, unknown senders.

    ``entries`` are ``(handle, tier, mode)`` rows; ``unknown`` are
    ``(handle, count, last_seen)`` rows from the metadata-only log — content-free
    by construction, so there is nothing to show beyond who and when.
    """
    rows = "".join(
        "<tr>"
        f"<td>{_esc(handle)}</td><td>{_esc(tier)}</td>"
        + (
            "<td>"
            '<form method="post" action="/settings/imessage">'
            f'<input type="hidden" name="handle" value="{_esc(handle)}">'
            '<input type="hidden" name="action" value="mode">'
            '<input type="hidden" name="mode" value="'
            + ("auto" if mode == "draft" else "draft")
            + '">'
            + f"<button>{'draft-first' if mode == 'draft' else 'auto'}"
            " ⇄</button></form></td>"
            if tier == "guest"
            else "<td>—</td>"
        )
        + '<td><form method="post" action="/settings/imessage">'
        f'<input type="hidden" name="handle" value="{_esc(handle)}">'
        '<input type="hidden" name="action" value="remove">'
        '<button class="danger">Remove</button></form></td>'
        "</tr>"
        for handle, tier, mode in entries
    )
    table = (
        "<table><tr><th>Handle</th><th>Tier</th><th>Mode</th><th></th></tr>"
        f"{rows}</table>"
        if rows
        else '<p class="note">No whitelisted handles yet.</p>'
    )
    unknown_rows = "".join(
        "<tr>"
        f"<td>{_esc(handle)}</td><td>{count}</td>"
        f'<td class="note">{_esc(last_seen)}</td>'
        '<td><form method="post" action="/settings/imessage">'
        f'<input type="hidden" name="handle" value="{_esc(handle)}">'
        '<input type="hidden" name="action" value="add">'
        "<button>Allow</button></form></td>"
        "</tr>"
        for handle, count, last_seen in unknown
    )
    unknown_html = (
        "<h3>Unknown senders</h3>"
        '<p class="note">Texted chief without being whitelisted — logged by'
        " handle and time only; their messages were never read.</p>"
        "<table><tr><th>Handle</th><th>Msgs</th><th>Last seen</th><th></th>"
        f"</tr>{unknown_rows}</table>"
        if unknown_rows
        else ""
    )
    return (
        '<div class="panel"><h2>iMessage whitelist</h2>'
        '<p class="note">Only whitelisted handles reach chief. Guests enter'
        " under guest limits; draft-first holds every outbound text for your"
        " approval. Changes apply immediately.</p>"
        + table
        + '<form class="stack" method="post" action="/settings/imessage">'
        '<input type="hidden" name="action" value="add">'
        '<input type="text" name="handle"'
        ' placeholder="+15551234567 or email">'
        '<label><input type="checkbox" name="draft"> Draft-first</label>'
        "<button>Whitelist handle</button></form>"
        + unknown_html
        + "</div>"
    )


def settings_page_body(
    *,
    telegram_connected: bool,
    discord_connected: bool,
    openrouter_connected: bool,
    current: Mapping[str, object],
    imessage_html: str = "",
    error: str | None = None,
) -> str:
    """The curated settings forms — never a general config editor."""
    banner = f'<p class="error">{_esc(error)}</p>' if error else ""
    openrouter = (
        '<div class="panel"><h2>OpenRouter</h2>'
        + (
            '<p><span class="ok">✔ Key saved</span></p>'
            '<form method="post" action="/settings/openrouter">'
            '<input type="hidden" name="action" value="disconnect">'
            '<button class="danger">Remove key</button></form>'
            if openrouter_connected
            else '<p class="note">Powers model routing, the cheap classifiers, and'
            " injection screening.</p>"
            '<form class="stack" method="post" action="/settings/openrouter">'
            '<input type="text" name="api_key" placeholder="OpenRouter API key">'
            "<button>Validate &amp; save</button></form>"
        )
        + "</div>"
    )
    lan_on = bool(current.get("web_lan_enabled"))
    model = str(current.get("owner_model_default") or "")
    start = str(current.get("quiet_hours_start") or "")
    end = str(current.get("quiet_hours_end") or "")
    return (
        "<h1>Settings</h1>"
        '<p class="note">Changes apply on the next daemon restart.</p>'
        + banner
        + _platform_section("Telegram", "telegram", telegram_connected)
        + _platform_section("Discord", "discord", discord_connected)
        + imessage_html
        + openrouter
        + '<div class="panel"><h2>Model</h2>'
        '<form class="stack" method="post" action="/settings/model">'
        f'<input type="text" name="owner_model_default" value="{_esc(model)}">'
        "<button>Save default model</button></form></div>"
        + '<div class="panel"><h2>Web access</h2>'
        '<form class="stack" method="post" action="/settings/web">'
        "<label><input type=\"checkbox\" name=\"lan\""
        f"{' checked' if lan_on else ''}> Serve on the LAN (all interfaces)"
        "</label><button>Save</button></form></div>"
        + '<div class="panel"><h2>Quiet hours</h2>'
        '<form class="stack" method="post" action="/settings/quiet-hours">'
        f'<input type="text" name="start" value="{_esc(start)}"'
        ' placeholder="Start (HH:MM, empty = off)">'
        f'<input type="text" name="end" value="{_esc(end)}"'
        ' placeholder="End (HH:MM)">'
        "<button>Save quiet hours</button></form></div>"
        + '<div class="panel"><h2>Password</h2>'
        '<p class="note">Changing it logs every browser out (including old'
        " sessions everywhere).</p>"
        '<form class="stack" method="post" action="/settings/password">'
        '<input type="password" name="current" placeholder="Current password"'
        ' autocomplete="current-password">'
        '<input type="password" name="password" placeholder="New password"'
        ' autocomplete="new-password">'
        '<input type="password" name="confirm" placeholder="Confirm new password"'
        ' autocomplete="new-password">'
        "<button>Change password</button></form></div>"
    )


def _thread_query(platform: str, thread_key: str) -> str:
    """URL-encoded ``platform``/``thread_key`` pair, HTML-escaped for attributes."""
    return _esc(urlencode({"platform": platform, "thread_key": thread_key}))


def thread_list_html(
    threads: Iterable[Mapping[str, object]],
    *,
    active_platform: str,
    active_thread: str,
) -> str:
    """The horizontal thread strip: every platform's live threads + New."""
    items = []
    seen_active = False
    for t in threads:
        platform, thread_key = str(t.get("platform")), str(t.get("thread_key"))
        is_active = platform == active_platform and thread_key == active_thread
        seen_active = seen_active or is_active
        title = str(t.get("title") or thread_key)
        status = str(t.get("status") or "")
        items.append(
            f'<a href="/chat?{_thread_query(platform, thread_key)}"'
            f'{" class=\"active\"" if is_active else ""}>'
            f"{_esc(title)}"
            f'{" ⏳" if status == "running" else ""}</a>'
        )
    if not seen_active:
        items.insert(
            0,
            f'<a class="active" href="/chat?'
            f'{_thread_query(active_platform, active_thread)}">'
            f"{_esc(active_thread)}</a>",
        )
    items.append('<a href="/chat/new">＋ New</a>')
    return "".join(items)


def history_message_html(message: Mapping[str, object]) -> str:
    """Render one #134 backfill row (role/kind/text/filename) as a transcript entry."""
    kind = str(message.get("kind") or "")
    text = str(message.get("text") or "")
    if kind == "milestone":
        return message_html("milestone", text)
    if kind == "file":
        filename = str(message.get("filename") or "file")
        return f'<div class="msg chief file">📄 {_esc(filename)} {_esc(text)}</div>'
    role = "owner" if message.get("role") == "owner" else "chief"
    return message_html(kind, text, role=role)


def chat_page_body(
    *,
    platform: str,
    thread_key: str,
    threads: Iterable[Mapping[str, object]],
    history: Iterable[Mapping[str, object]],
    cards: Iterable[Mapping[str, object]],
) -> str:
    """The whole chat surface: thread strip, approvals, transcript, composer."""
    query = _thread_query(platform, thread_key)
    transcript = "".join(history_message_html(m) for m in history)
    cancel_form = (
        f'<form hx-post="/chat/cancel" hx-swap="none">'
        f'<input type="hidden" name="platform" value="{_esc(platform)}">'
        f'<input type="hidden" name="thread_key" value="{_esc(thread_key)}">'
        '<button class="quiet" title="Cancel the running task">✕ Cancel task'
        "</button></form>"
        if platform == "cli"
        else ""
    )
    return (
        f'<div class="chat" hx-ext="sse" sse-connect="/events?{query}">'
        f'<div class="threads" hx-get="/chat/threads?{query}"'
        ' hx-trigger="every 5s" hx-swap="innerHTML">'
        + thread_list_html(
            threads, active_platform=platform, active_thread=thread_key
        )
        + "</div>"
        '<div id="approvals" sse-swap="approvals" hx-swap="innerHTML">'
        + approvals_html(cards)
        + "</div>"
        '<div id="transcript" sse-swap="message" hx-swap="beforeend">'
        + transcript
        + "</div>"
        f'<form class="composer" hx-post="/chat/send" hx-target="#transcript"'
        ' hx-swap="beforeend" hx-on::after-request="this.reset();'
        "document.getElementById('transcript').scrollTop = 1e9\">"
        f'<input type="hidden" name="platform" value="{_esc(platform)}">'
        f'<input type="hidden" name="thread_key" value="{_esc(thread_key)}">'
        '<input type="text" name="text" autocomplete="off" autofocus'
        ' placeholder="Message chief — /commands work too">'
        "<button>Send</button></form>"
        f"{cancel_form}"
        "</div>"
    )
