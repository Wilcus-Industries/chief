"""JS for the transcript's live stream: the SSE client, the history re-render,
client-only (ephemeral) rows, and scroll-follow.

The transcript is rebuilt from stored history only on a buffer switch or an
SSE reconnect — never per frame — so nothing streamed live is wiped mid-turn.
A slash command and its answer are never stored, so they are kept here as
*ephemeral* rows, each anchored to how many stored rows preceded it, and
re-placed after a re-render. The view follows new output only while the owner
is already at the bottom; scrolled up, a "latest" button appears instead.
Concatenated into /app.js (one global scope).
"""

# Pure (no DOM), so tests run it under node on its own.
STREAM_PURE_SCRIPT = """
/* a collapsed activity group's one-line label: the newest tool call's name */
function groupLabel(tools){
  if (!tools.length) return "thinking";
  if (tools.length === 1) return tools[0];
  return `${tools.length} tool calls \\u00b7 ${tools[tools.length - 1]}`;
}

/* within a few lines of the bottom counts as "following the output" */
function nearBottom(box){
  return box.scrollHeight - box.scrollTop - box.clientHeight < 48;
}
"""

STREAM_SCRIPT = STREAM_PURE_SCRIPT + """
let es = null, wasDown = false, liveThink = null, pinned = true;
const ephemeral = {};  /* thread -> [{anchor, role, text, node}] never stored */
const cmdWait = {};    /* thread -> slash-command answers still to arrive */

log.addEventListener("scroll", () => {
  pinned = nearBottom(log); if (pinned) el("jump").hidden = true;
});
el("jump").onclick = () => toBottom();
function follow(){
  if (pinned) log.scrollTop = log.scrollHeight; else el("jump").hidden = false;
}
function toBottom(){
  pinned = true; log.scrollTop = log.scrollHeight; el("jump").hidden = true;
}

const storedRows = () => [...log.children].filter(
  (n) => !n.classList.contains("eph") && !n.classList.contains("empty"));

/* a row the server never stores: shown now, re-placed after each re-render */
function addEph(role, text){
  const e = {anchor: storedRows().length, role, text};
  (ephemeral[state.current] ||= []).push(e); e.node = ephNode(e); return e;
}
function ephNode(e){
  const body = addMsg(e.role, e.text), row = body.parentNode;
  row.classList.add("eph"); return row;
}
function dropEph(tk, e){
  const list = ephemeral[tk] || [], i = list.indexOf(e);
  if (i >= 0) list.splice(i, 1);
  if (e.node) e.node.remove();
}
function placeEphemeral(tk){
  const base = storedRows();
  (ephemeral[tk] || []).forEach((e) => {
    e.node = ephNode(e);
    const ref = base[e.anchor]; if (ref) log.insertBefore(e.node, ref);
  });
}

function bumpThread(thread, preview){
  if (preview != null) snippets[thread] = preview;
  const i = state.sessions.findIndex((s) => s.thread === thread);
  if (i < 0){ loadSessions(); return; }            /* unknown thread: refetch */
  state.sessions.unshift(state.sessions.splice(i, 1)[0]);  /* newest-first */
  if (thread !== state.current) unread.add(thread);
  renderBuffers();
}

/* canonical re-render of a thread from stored history; only the newest call
   may paint, so a slow fetch can't overwrite a later one */
let reloadSeq = 0;
async function reloadHistory(tk, keepScroll){
  const seq = ++reloadSeq;
  const rows = await getJSON("/history?thread=" + encodeURIComponent(tk));
  if (seq !== reloadSeq || tk !== state.current) return;
  const top = log.scrollTop;
  live = null; liveThink = null; log.replaceChildren();
  (rows || []).forEach((r) => {
    if (r.role === "tool") addTool(r.call_id, r.name);
    else if (r.role === "thinking") addThinking(r.text);
    else addMsg(r.role, r.text);
  });
  placeEphemeral(tk);
  if (!log.children.length)
    log.appendChild(span("empty", "no messages yet \\u2014 type below to start."));
  reconcilePending(pending[tk] || [],
    (rows || []).filter((r) => r.role === "owner").map((r) => r.text));
  renderPending();
  if (keepScroll && !pinned) log.scrollTop = top; else toBottom();
}

function connect(tk){
  es = new EventSource("/events?thread=" + encodeURIComponent(tk));
  const conn = el("conn"), lbl = el("conn-label");
  es.onopen = () => {
    conn.className = "seg live"; lbl.textContent = "live";
    if (wasDown){ wasDown = false; reloadHistory(state.current, true); }
  };
  es.onerror = () => {
    conn.className = "seg down"; lbl.textContent = "offline"; wasDown = true;
  };
  es.onmessage = onFrame;
}

/* switch the subscription to the newly focused thread */
function subscribe(tk){ if (es) es.close(); connect(tk); }

function onFrame(e){
  const f = JSON.parse(e.data);
  if (f.type === "tick"){ bumpThread(f.thread, f.preview); return; }
  if (f.thread !== state.current) return;   /* rich frame racing a buffer switch */
  /* any frame but more thinking ends the thinking block; inbound/tool/thinking
     close the streaming bubble, so later text lands below them as history has it */
  if (f.type !== "thinking") liveThink = null;
  if (f.type === "inbound"){
    live = null; cmdWait[f.thread] = 0; onInbound(f.thread, f.text);
  } else if (f.type === "tool"){ live = null; addTool(f.call_id, f.name); }
  else if (f.type === "thinking"){
    live = null; if (!liveThink) liveThink = addThinking("");
    liveThink.textContent += f.text;
  }
  else if (f.type === "approval") addApproval(f.thread, f.question);
  else if (f.type === "approval_resolved") resolveApproval(f.thread);
  else if (f.type === "delta"){
    if (!live){ live = addMsg("chief", ""); liveText = ""; }
    liveText += f.text; setMarkdown(live, liveText);  /* re-parse: marks span deltas */
  } else if (f.type === "final") onFinal(f);
  follow();
}

function onFinal(f){
  /* a slash command's answer is not a turn: show it, but it is never stored */
  if (!live && cmdWait[f.thread] > 0){
    cmdWait[f.thread]--; addEph("chief", f.text); return;
  }
  /* keep the streamed text: with tool frames off, one bubble holds several
     model calls and the final carries only the last */
  if (live){ setMarkdown(live, liveText || f.text); live = null; }
  else addMsg("chief", f.text);
  bumpThread(f.thread, f.text);
}
"""
