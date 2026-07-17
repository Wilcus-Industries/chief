"""The web UI client, served at /app.js. Buffers, SSE, readline completion."""

SCRIPT = """
const el = (id) => document.getElementById(id);
const log = el("log"), input = el("input"), pop = el("complete");
const state = {current: null, sessions: [], commands: [], matches: [], sel: -1};
const unread = new Set();
let live = null;

const bare = (tk) => tk.startsWith("web:") ? tk.slice(4) : null;
const label = (tk) => bare(tk) ?? tk;
const span = (cls, text) => {
  const s = document.createElement("span");
  if (cls) s.className = cls;
  if (text != null) s.textContent = text;
  return s;
};
async function getJSON(url){ const r = await fetch(url);
  return r.ok ? r.json() : null; }

/* ---- buffers ---- */
function renderBuffers(){
  const list = el("buflist"); list.replaceChildren();
  state.sessions.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = (s.thread === state.current ? "active " : "") +
      (unread.has(s.thread) ? "unread" : "");
    li.append(span("idx", i), span("name", label(s.thread)));
    if (s.channel !== "web") li.append(span("badge", s.channel));
    li.onclick = () => switchTo(s.thread);
    list.appendChild(li);
  });
}

function setStatus(tk){
  const s = state.sessions.find((x) => x.thread === tk);
  el("buf-seg").textContent = label(tk);
  el("model-seg").textContent = "model " + ((s && s.model) || "default");
  const ro = s && s.channel !== "web";
  el("readonly").hidden = !ro;
  el("f").classList.toggle("disabled", !!ro);
  input.disabled = !!ro;
}

async function switchTo(tk){
  state.current = tk; live = null; unread.delete(tk);
  setStatus(tk); renderBuffers();
  const rows = await getJSON("/history?thread=" + encodeURIComponent(tk)) || [];
  log.replaceChildren();
  if (!rows.length){
    const d = span("empty", "no messages yet \\u2014 type below to start.");
    log.appendChild(d);
  }
  rows.forEach((r) => addMsg(r.role, r.text));
  if (!input.disabled) input.focus();
}

function addMsg(role, text){
  const empty = log.querySelector(".empty"); if (empty) empty.remove();
  const d = document.createElement("div"); d.className = "msg " + role;
  const body = span(null, text);
  d.append(span("who"), body); log.appendChild(d);
  log.scrollTop = log.scrollHeight; return body;
}

/* ---- live stream ---- */
function connect(){
  const es = new EventSource("/events");
  const conn = el("conn"), lbl = el("conn-label");
  es.onopen = () => { conn.className = "seg live"; lbl.textContent = "live"; };
  es.onerror = () => { conn.className = "seg down"; lbl.textContent = "offline"; };
  es.onmessage = (e) => {
    const f = JSON.parse(e.data);
    if (!state.sessions.some((s) => s.thread === f.thread)){ loadSessions(); return; }
    if (f.thread !== state.current){ unread.add(f.thread); renderBuffers(); return; }
    if (f.type === "delta"){
      if (!live) live = addMsg("chief", ""); live.textContent += f.text;
    } else if (f.type === "final"){
      if (live){ live.textContent = f.text; live = null; } else addMsg("chief", f.text);
    }
    log.scrollTop = log.scrollHeight;
  };
}

async function loadSessions(){
  const rows = await getJSON("/sessions") || [];
  if (!rows.some((s) => s.thread === "web:main"))
    rows.unshift({thread: "web:main", channel: "web", model: null, count: 0});
  state.sessions = rows; renderBuffers();
  if (!state.current || !rows.some((s) => s.thread === state.current)){
    const start = rows.find((s) => s.channel === "web") || rows[0];
    switchTo(state.current || start.thread);
  }
}

/* ---- send ---- */
el("f").onsubmit = async (e) => {
  e.preventDefault();
  if (acceptCompletion()) return;
  const text = input.value.trim(); if (!text || input.disabled) return;
  addMsg("owner", text); input.value = ""; hidePop();
  await fetch("/send", {method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({thread: bare(state.current), text})});
};

/* ---- readline-style completion ---- */
function showPop(){
  const q = input.value;
  state.matches = (q.startsWith("/") && !q.includes(" "))
    ? state.commands.filter((c) => c.startsWith(q)) : [];
  if (!state.matches.length){ hidePop(); return; }
  state.sel = 0; pop.hidden = false; pop.replaceChildren();
  state.matches.forEach((c, i) => {
    const li = document.createElement("li");
    if (i === 0) li.className = "sel";
    li.append(span("hit", c.slice(0, q.length)), span(null, c.slice(q.length)));
    li.onmousedown = (ev) => {
      ev.preventDefault(); state.sel = i; acceptCompletion();
    };
    pop.appendChild(li);
  });
}
function hidePop(){ pop.hidden = true; state.matches = []; state.sel = -1; }
function moveSel(step){
  const items = pop.children; if (!items.length) return;
  items[state.sel] && items[state.sel].classList.remove("sel");
  state.sel = (state.sel + step + items.length) % items.length;
  items[state.sel].classList.add("sel");
}
function acceptCompletion(){
  if (pop.hidden || state.sel < 0) return false;
  input.value = state.matches[state.sel] + " "; hidePop(); input.focus();
  return true;
}

input.addEventListener("input", showPop);
input.addEventListener("keydown", (e) => {
  if (pop.hidden) return;
  if (e.key === "ArrowDown"){ e.preventDefault(); moveSel(1); }
  else if (e.key === "ArrowUp"){ e.preventDefault(); moveSel(-1); }
  else if (e.key === "Tab"){ e.preventDefault(); acceptCompletion(); }
  else if (e.key === "Escape"){ e.preventDefault(); hidePop(); }
});

/* ---- new buffer + keybinds ---- */
el("newbuf").onclick = () => {
  const name = (prompt("new buffer name") || "").trim(); if (!name) return;
  const tk = "web:" + name;
  if (!state.sessions.some((s) => s.thread === tk))
    state.sessions.unshift({thread: tk, channel: "web", model: null, count: 0});
  switchTo(tk);
};
document.addEventListener("keydown", (e) => {
  if (e.altKey && /^[0-9]$/.test(e.key)){
    const s = state.sessions[+e.key]; if (s){ e.preventDefault(); switchTo(s.thread); }
  }
});

async function monitors(){
  const r = await fetch("/monitors"); const t = (await r.text()).trim();
  el("mon-seg").textContent = "mon " + (t === "none" ? 0 : t.split(";").length);
}

/* ---- boot ---- */
(async () => {
  state.commands = await getJSON("/commands") || [];
  await loadSessions(); connect(); monitors(); setInterval(monitors, 10000);
})();
"""
