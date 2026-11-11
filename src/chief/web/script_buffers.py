"""JS for the buffer sidebar: the list, delete, the inline new-buffer field,
and the narrow-screen drawer. Only web scratch buffers can be deleted — the
same rule the ``/delete`` route enforces; the primary and every real channel
are permanent. Split out of script.py (length cap); concatenated into /app.js.
"""

# Pure (no DOM), so tests run it under node on its own.
BUFFERS_PURE_SCRIPT = r"""
/* mirrors /delete: web scratch buffers only, never the primary */
const deletable = (s) => s.channel === "web" && s.thread !== "web:main";

/* a reply preview for the sidebar: markdown marks and line breaks dropped */
function plainSnippet(text){
  return text
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+/gm, "")
    .replace(/[*_`~]/g, "")
    .replace(/\s+/g, " ").trim();
}
"""

BUFFERS_SCRIPT = BUFFERS_PURE_SCRIPT + """
function renderBuffers(){
  const list = el("buflist"); list.replaceChildren();
  state.sessions.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = (s.thread === state.current ? "active " : "") +
      (unread.has(s.thread) ? "unread" : "");
    li.tabIndex = 0; li.title = s.thread;
    li.append(span("idx", i), span("name", label(s.thread)));
    if (s.channel !== "web") li.append(span("badge", s.channel));
    if (deletable(s)){
      const x = document.createElement("button");
      x.type = "button"; x.className = "kill"; x.textContent = "\\u00d7";
      x.title = "delete buffer " + label(s.thread);
      x.setAttribute("aria-label", x.title);
      x.onclick = (e) => { e.stopPropagation(); delBuf(s.thread); };
      li.append(x);
    }
    const snip = snippets[s.thread];
    if (snip) li.append(span("snippet", plainSnippet(snip)));
    li.onclick = () => switchTo(s.thread);
    li.onkeydown = (e) => {
      if (e.key === "Enter" && e.target === li) switchTo(s.thread);
    };
    list.appendChild(li);
  });
}

async function delBuf(tk){
  if (!confirm(`Delete buffer "${label(tk)}"? Its transcript is erased.`)) return;
  let reason = null;
  try {
    const r = await fetch("/delete", {method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({thread: tk})});
    if (!r.ok) reason = (await r.text()) || "server error";
  } catch (err) { reason = "network error"; }
  if (reason){ notice(`not deleted \\u2014 ${reason}`, "bad"); return; }
  unread.delete(tk); delete ephemeral[tk];
  if (state.current === tk) await switchTo("web:main");
  await loadSessions(); notice(`deleted ${label(tk)}`);
}

async function loadSessions(){
  const rows = await getJSON("/sessions") || [];
  if (!rows.some((s) => s.thread === "web:main"))
    rows.unshift({thread: "web:main", channel: "web", model: null, count: 0});
  /* a new scratch buffer lives client-side until its first turn stores it */
  state.sessions.filter((s) => s.thread === state.current && deletable(s))
    .forEach((s) => { if (!rows.some((x) => x.thread === s.thread)) rows.unshift(s); });
  state.sessions = rows; renderBuffers();
  if (!state.current || !rows.some((s) => s.thread === state.current)){
    const start = rows.find((s) => s.channel === "web") || rows[0];
    switchTo(state.current || start.thread);
  }
}

/* "+ new" turns into a name field: Enter opens the buffer, Esc cancels */
el("newbuf").onclick = () => {
  const btn = el("newbuf"), field = document.createElement("input");
  field.id = "newname"; field.placeholder = "buffer name \\u21b5";
  btn.hidden = true; btn.after(field); field.focus();
  let open = true;  /* removing the field fires blur: close once */
  const done = () => { if (open){ open = false; field.remove(); btn.hidden = false; } };
  field.onblur = done;
  field.onkeydown = (e) => {
    if (e.key === "Escape"){ e.preventDefault(); done(); return; }
    if (e.key !== "Enter") return;
    e.preventDefault();
    const name = field.value.trim(); done(); if (!name) return;
    const tk = "web:" + name;
    if (!state.sessions.some((s) => s.thread === tk))
      state.sessions.unshift({thread: tk, channel: "web", model: null, count: 0});
    switchTo(tk);
  };
};

/* narrow screens: the sidebar is a drawer behind the status bar's menu button */
el("menu").onclick = () => document.body.classList.toggle("drawer");
const closeDrawer = () => document.body.classList.remove("drawer");
"""
