"""Server-rendered HTML for the web UI (#153). No template engine, no Node.

Small pure functions building escaped HTML strings — the whole view layer. Pages share
one :func:`page` layout (phone-first, single inline stylesheet, htmx + its SSE
extension served from ``/static``); interactive routes return *fragments* the client
swaps in via htmx or SSE. Every interpolated value passes :func:`html.escape`.
"""

import html
from collections.abc import Iterable, Mapping

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
            f'<a href="/chat?platform={_esc(platform)}'
            f'&amp;thread_key={_esc(thread_key)}"'
            f'{" class=\"active\"" if is_active else ""}>'
            f"{_esc(title)}"
            f'{" ⏳" if status == "running" else ""}</a>'
        )
    if not seen_active:
        items.insert(
            0,
            f'<a class="active" href="/chat?platform={_esc(active_platform)}'
            f'&amp;thread_key={_esc(active_thread)}">{_esc(active_thread)}</a>',
        )
    return "".join(items)
