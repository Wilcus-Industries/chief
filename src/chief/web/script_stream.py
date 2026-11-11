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

/* a bubble's text at its final: what streamed, unless the final extends it —
   a reply joined mid-stream (buffer switch, SSE reconnect) holds only its tail.
   With tool frames off one bubble spans several model calls; the final is then
   only the last call, so the streamed text is kept. */
function finalText(streamed, final){
  const joinedLate = final.length > streamed.length && final.endsWith(streamed);
  return !streamed || joinedLate ? final : streamed;
}

/* client-only rows still placeable: anchored past the stored rows means the
   transcript shrank (/clear, compaction), and they belonged to what it lost */
const liveEphemeral = (list, stored) => list.filter((e) => e.anchor <= stored);
"""

STREAM_SCRIPT = STREAM_PURE_SCRIPT + """
let es = null, wasDown = false, liveThink = null, pinned = true;
const ephemeral = {};  /* thread -> [{anchor, role, text, node}] never stored */
const sentCmds = {};   /* thread -> this tab's command rows awaiting answers */

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
/* a turn is running on the focused thread: from its inbound frame to its final */
const working = (on) => { el("working").hidden = !on; };

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
  ephemeral[tk] = liveEphemeral(ephemeral[tk] || [], base.length);
  ephemeral[tk].forEach((e) => {
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
  live = null; liveThink = null; log.replaceChildren(); working(false);
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
  if (f.type === "command"){ onCommand(f); return; }  /* beside a turn, not in it */
  /* any frame but more thinking ends the thinking block; inbound/tool/thinking
     close the streaming bubble, so later text lands below them as history has it */
  if (f.type !== "thinking") liveThink = null;
  if (["inbound", "tool", "thinking", "delta"].includes(f.type)) working(true);
  if (f.type === "inbound"){ live = null; onInbound(f.thread, f.text); }
  else if (f.type === "tool"){ live = null; addTool(f.call_id, f.name); }
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
  if (live){ setMarkdown(live, finalText(liveText, f.text)); live = null; }
  else addMsg("chief", f.text);
  working(false); bumpThread(f.thread, f.text);
}

/* a slash command's answer: never stored, so the command and its answer are
   client-only rows. Idle, the thread re-renders first — /clear and /compact
   rewrite what is stored. A command sent from elsewhere arrives the same way. */
async function onCommand(f){
  const tk = f.thread, mine = (sentCmds[tk] || []).shift();
  if (mine) dropEph(tk, mine);
  if (!live && el("working").hidden) await reloadHistory(tk, true);
  if (tk !== state.current) return;
  addEph("owner", f.command); addEph("chief", f.text); follow();
}
"""
