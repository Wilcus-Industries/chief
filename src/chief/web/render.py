"""Server-rendered HTML for the web UI (#153). No template engine, no Node.

Small pure functions building escaped HTML strings — the whole view layer. Pages share
one :func:`page` layout (phone-first, single inline stylesheet, htmx + its SSE
extension served from ``/static``); interactive routes return *fragments* the client
swaps in via htmx or SSE. Every interpolated value passes :func:`html.escape`.
"""

import html
import json
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

#: One coherent visual system: fired-clay monochrome. A single hue family carries
#: everything — dark mode is smoke-fired blackware (deep brown-black ground, pale
#: clay ink, ochre accent), light mode is its inverse (sand ground, ink-brown text).
#: Monospace throughout (chief is a terminal-born daemon), square geometry, and one
#: signature motif: the woven zigzag band under the header and above the composer.
_STYLE = """
:root { color-scheme: light dark;
  --bg: #ece0cb; --fg: #40260f; --muted: #8a6b4a; --line: #cfb894;
  --panel: #f4ecdc; --accent: #a34e1a; --accent-fg: #f7efe0; --danger: #952d0e; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #191008; --fg: #e6c9a3; --muted: #99795c; --line: #40301f;
    --panel: #241812; --accent: #d98546; --accent-fg: #1c0f05; --danger: #e0603a; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  min-height: 100dvh; display: flex; flex-direction: column;
  font: 15px/1.5 ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas,
    "DejaVu Sans Mono", monospace; }
a { color: var(--accent); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
/* the signature band: a woven zigzag strip, drawn in CSS only */
.band { height: 8px; background:
  linear-gradient(135deg, var(--line) 25%, transparent 25%) -4px 0 / 8px 8px,
  linear-gradient(225deg, var(--line) 25%, transparent 25%) -4px 0 / 8px 8px; }
header { display: flex; align-items: baseline; gap: 1rem; padding: .6rem .9rem;
  position: sticky; top: 0; background: var(--bg); z-index: 5; flex-wrap: wrap; }
header::after { content: ""; position: absolute; left: 0; right: 0; bottom: -8px;
  height: 8px; background:
  linear-gradient(135deg, var(--line) 25%, transparent 25%) -4px 0 / 8px 8px,
  linear-gradient(225deg, var(--line) 25%, transparent 25%) -4px 0 / 8px 8px; }
header .brand { font-weight: 700; letter-spacing: .3em; text-transform: uppercase; }
header .brand::before { content: "\\25c6 "; color: var(--accent); }
nav { display: flex; gap: .75rem; margin-left: auto; }
nav a { color: var(--muted); text-decoration: none; padding: .1rem 0;
  text-transform: uppercase; letter-spacing: .08em; font-size: .78rem; }
nav a.active { color: var(--fg); border-bottom: 2px solid var(--accent); }
main { max-width: 44rem; margin: 0 auto; padding: 1.1rem .9rem .9rem;
  width: 100%; flex: 1; display: flex; flex-direction: column; }
main > * { flex: 0 0 auto; }
/* login/setup: no nav anywhere to go — one centered brand block instead */
body.bare main { justify-content: center; padding-bottom: 5rem; }
.auth-brand { text-align: center; font-weight: 700; letter-spacing: .5em;
  text-transform: uppercase; font-size: 1.35rem; text-indent: .5em; }
.auth-brand::before { content: "\\25c6"; display: block; color: var(--accent);
  letter-spacing: 0; text-indent: 0; margin-bottom: .5rem; }
.auth-brand + .band { margin: 1rem 0 1.6rem; }
body.bare h1 { text-align: center; }
h1 { font-size: .95rem; margin: .2rem 0 .8rem; text-transform: uppercase;
  letter-spacing: .14em; color: var(--muted); font-weight: 600; }
h1::before { content: "\\25c6 "; color: var(--accent); font-size: .7em; }
h2 { font-size: .85rem; margin: .2rem 0 .5rem; text-transform: uppercase;
  letter-spacing: .1em; font-weight: 600; }
h3 { font-size: .85rem; text-transform: uppercase; letter-spacing: .1em; }
form.stack { display: flex; flex-direction: column; gap: .55rem; }
input, select, button, textarea { font: inherit; color: inherit; }
input[type=text], input[type=password], input[type=file], select, textarea {
  width: 100%; padding: .55rem .65rem; border: 1px solid var(--line);
  border-radius: 0; background: var(--panel); }
input[type=file]::file-selector-button { font: inherit; font-size: .78rem;
  margin-right: .6rem; padding: .15rem .6rem; border: 1px solid var(--line);
  border-radius: 0; background: var(--bg); color: var(--muted); cursor: pointer;
  text-transform: uppercase; letter-spacing: .06em; }
input::placeholder { color: var(--muted); }
button { padding: .55rem .9rem; border: 1px solid var(--accent); border-radius: 0;
  background: var(--accent); color: var(--accent-fg); cursor: pointer;
  text-transform: uppercase; letter-spacing: .08em; font-size: .8rem; }
button.quiet { background: transparent; color: var(--muted);
  border: 1px solid var(--line); }
button.danger { background: transparent; color: var(--danger);
  border: 1px solid var(--danger); }
.error { color: var(--danger); margin: .4rem 0; }
.error::before { content: "\\2717 "; }
.note { color: var(--muted); font-size: .85rem; }
.panel { background: var(--panel); border: 1px solid var(--line);
  padding: .8rem .9rem; margin: .7rem 0; }
/* chat */
.chat { display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; }
.threads { display: flex; gap: .4rem; overflow-x: auto; padding: .5rem 0 .4rem; }
.threads a { white-space: nowrap; text-decoration: none; color: var(--muted);
  border: 1px solid var(--line); padding: .2rem .7rem; font-size: .8rem;
  background: var(--panel); }
.threads a.active { color: var(--accent); border-color: var(--accent); }
#transcript { flex: 1; overflow-y: auto; display: flex;
  flex-direction: column; gap: .5rem; padding: .4rem 0; }
.msg { padding: .5rem .7rem; max-width: 92%;
  overflow-wrap: break-word; white-space: pre-wrap; }
.msg.owner { align-self: flex-end; background: var(--accent);
  color: var(--accent-fg); }
.msg.chief { align-self: flex-start; background: var(--panel);
  border: 1px solid var(--line); border-left: 3px solid var(--accent); }
.msg.streaming::after { content: "\\258a"; color: var(--accent);
  animation: blink 1s step-end infinite; }
.msg.milestone { align-self: flex-start; color: var(--muted);
  font-size: .85rem; padding: 0 .7rem; }
.msg.file a { color: inherit; }
/* live tool chips */
.tool { align-self: flex-start; font-size: .8rem; color: var(--muted);
  border: 1px dashed var(--line); padding: .15rem .6rem;
  text-transform: uppercase; letter-spacing: .06em; }
.tool::before { content: "\\2699 "; }
.tool.running::after { content: " \\2026"; color: var(--accent);
  animation: blink 1s step-end infinite; }
.tool.ok { border-style: solid; }
.tool.ok::before { content: "\\2713 "; color: var(--accent); }
.tool.fail { border-color: var(--danger); color: var(--danger);
  border-style: solid; }
.tool.fail::before { content: "\\2717 "; }
.tool .tool-detail { text-transform: none; letter-spacing: 0;
  margin-left: .5rem; }
@keyframes blink { 50% { opacity: 0; } }
@media (prefers-reduced-motion: reduce) {
  .msg.streaming::after, .tool.running::after { animation: none; } }
/* approval cards */
.card { border: 1px solid var(--accent); padding: .7rem .8rem; margin: .4rem 0;
  background: var(--panel); }
.card > .note::before { content: "\\25c6 "; color: var(--accent); }
.card .actions { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .55rem; }
.card button { font-size: .78rem; padding: .4rem .6rem; }
.card-elsewhere { display: block; font-size: .85rem; color: var(--muted);
  border: 1px dashed var(--line); padding: .35rem .7rem; margin: .3rem 0;
  text-decoration: none; }
/* composer + command menu */
.composer { display: flex; gap: .5rem; position: sticky; bottom: 0;
  background: var(--bg); padding: .6rem 0 .5rem; }
.composer::before { content: ""; position: absolute; left: 0; right: 0; top: -8px;
  height: 8px; background:
  linear-gradient(135deg, var(--line) 25%, transparent 25%) -4px 0 / 8px 8px,
  linear-gradient(225deg, var(--line) 25%, transparent 25%) -4px 0 / 8px 8px; }
.composer input { flex: 1; }
#cmd-menu { position: sticky; bottom: 4.3rem; background: var(--panel);
  border: 1px solid var(--line); max-height: 14rem; overflow-y: auto;
  margin-bottom: 8px; /* clear the composer's woven band drawn at top:-8px */ }
.cmd-item { display: flex; gap: .7rem; align-items: baseline;
  padding: .5rem .7rem; cursor: pointer; }
.cmd-item.selected { background: var(--bg); border-left: 3px solid var(--accent); }
.cmd-item .cmd-name { color: var(--accent); font-weight: 600;
  white-space: nowrap; }
.cmd-item .cmd-desc { color: var(--muted); font-size: .85rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* tables */
table { width: 100%; border-collapse: collapse; }
td, th { text-align: left; padding: .4rem .3rem;
  border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .78rem;
  text-transform: uppercase; letter-spacing: .08em; }
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
        '<script src="/static/chief.js" defer></script>'
        "</head><body>"
        f'<header><span class="brand">chief</span><nav>{nav}</nav></header>'
        f"<main>{body}</main>"
        "</body></html>"
    )


def bare_page(title: str, body: str) -> str:
    """A layout without the nav — for login/setup, where nothing else is reachable.

    No header either: a single centered brand block over the woven band carries
    the identity, and the form hangs from it in the middle of the screen.
    """
    return (
        "<!doctype html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)} — chief</title>"
        f"<style>{_STYLE}</style>"
        '</head><body class="bare">'
        '<main><div class="auth-brand">chief</div><div class="band"></div>'
        f"{body}</main>"
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


def card_html(
    card: Mapping[str, object],
    *,
    active_platform: str = "",
    active_thread: str = "",
) -> str:
    """An actionable approval card: preview text + the four answer buttons.

    The answer POST carries the page's active thread so the response re-renders
    the approvals block scoped the same way this page was.
    """
    approval_id = _esc(card.get("approval_id"))
    options = card.get("options")
    context = (
        f', "platform": "{_esc(active_platform)}"'
        f', "thread_key": "{_esc(active_thread)}"'
        if active_platform
        else ""
    )
    buttons = "".join(
        f'<button hx-post="/approvals/{approval_id}"'
        f' hx-vals=\'{{"action": "{_esc(o["action"])}"{context}}}\''
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


def _foreign_card_link(card: Mapping[str, object]) -> str:
    """A one-line pointer to an approval waiting in another thread."""
    platform = str(card.get("platform"))
    thread_key = str(card.get("thread_key"))
    return (
        f'<a class="card-elsewhere" href="/chat?'
        f'{_thread_query(platform, thread_key)}">'
        f"⧗ approval waiting in {_esc(platform)} · {_esc(thread_key)}</a>"
    )


def approvals_html(
    cards: Iterable[Mapping[str, object]],
    *,
    active_platform: str = "",
    active_thread: str = "",
) -> str:
    """The pending-approvals block (swapped whole on every card event).

    Scoped to the page's active thread: only that thread's cards render with
    answer buttons here; a card pending in any other thread shows as a compact
    link to its own chat page, so answer-from-anywhere stays one tap away
    without foreign requests masquerading as this conversation's. With no
    active context (no filter), every card renders in full.
    """
    full, elsewhere = [], []
    for card in cards:
        matches = not active_platform or (
            str(card.get("platform")) == active_platform
            and str(card.get("thread_key")) == active_thread
        )
        if matches:
            full.append(
                card_html(
                    card,
                    active_platform=active_platform,
                    active_thread=active_thread,
                )
            )
        else:
            elsewhere.append(_foreign_card_link(card))
    return "".join(full) + "".join(elsewhere)


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


def watches_section(
    entries: Iterable[tuple[str, str, str, str, str]],
) -> str:
    """Read-only watches table (#165): manage from chat or /watches, not here."""
    rows = "".join(
        f"<tr><td>{_esc(target)}</td><td>{_esc(instruction)}</td>"
        f"<td>{_esc(expiry)}</td><td>{_esc(tone)}</td><td>{_esc(state)}</td></tr>"
        for target, instruction, expiry, tone, state in entries
    )
    table = (
        "<table><tr><th>Target</th><th>Instruction</th><th>Expiry</th>"
        f"<th>Tone</th><th>State</th></tr>{rows}</table>"
        if rows
        else '<p class="note">No watches.</p>'
    )
    return (
        '<div class="panel"><h2>Watches</h2>'
        '<p class="note">Read-only — create or cancel a watch by talking to'
        " chief, or with /watches.</p>" + table + "</div>"
    )


def settings_page_body(
    *,
    telegram_connected: bool,
    discord_connected: bool,
    openrouter_connected: bool,
    current: Mapping[str, object],
    imessage_html: str = "",
    watches_html: str = "",
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
        + watches_html
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


def commands_json(commands: Iterable[tuple[str, str]]) -> str:
    """The slash-command palette as embeddable JSON (for the composer dropdown).

    ``</`` is escaped so the payload can never close its ``<script>`` container.
    """
    payload = json.dumps(
        [{"name": f"/{name}", "desc": desc} for name, desc in commands]
    )
    return payload.replace("</", "<\\/")


def chat_page_body(
    *,
    platform: str,
    thread_key: str,
    threads: Iterable[Mapping[str, object]],
    history: Iterable[Mapping[str, object]],
    cards: Iterable[Mapping[str, object]],
    commands: Iterable[tuple[str, str]] = (),
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
        f'<div class="chat" data-events-url="/events?{query}">'
        f'<div class="threads" hx-get="/chat/threads?{query}"'
        ' hx-trigger="every 5s" hx-swap="innerHTML">'
        + thread_list_html(
            threads, active_platform=platform, active_thread=thread_key
        )
        + "</div>"
        '<div id="approvals">'
        + approvals_html(
            cards, active_platform=platform, active_thread=thread_key
        )
        + "</div>"
        '<div id="transcript">'
        + transcript
        + "</div>"
        '<div id="cmd-menu" role="listbox" hidden></div>'
        f'<form class="composer" hx-post="/chat/send" hx-target="#transcript"'
        ' hx-swap="beforeend" hx-on::after-request="this.reset();'
        "document.getElementById('transcript').scrollTop = 1e9\">"
        f'<input type="hidden" name="platform" value="{_esc(platform)}">'
        f'<input type="hidden" name="thread_key" value="{_esc(thread_key)}">'
        '<input type="text" name="text" autocomplete="off" autofocus'
        ' placeholder="Message chief — / for commands">'
        "<button>Send</button></form>"
        f"{cancel_form}"
        '<script type="application/json" id="cmd-data">'
        + commands_json(commands)
        + "</script>"
        "</div>"
    )
