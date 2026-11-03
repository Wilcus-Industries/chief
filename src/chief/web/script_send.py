"""JS for sending: the queued/interrupt pending list and the send keybinds.

A sent message is held as *pending* — shown dimmed below the transcript, in
its own container so a history re-render can never wipe it — until the server
streams its ``inbound`` frame (the turn actually took the text), and only then
joins the transcript. Enter queues behind a running turn; Ctrl/Cmd+Enter
interrupts it (the agent sees the text at its next tool boundary). On a touch
device there is no Ctrl key, so Enter interrupts there. Split out of script.py
to keep that file under the length cap; concatenated into one global scope.
"""

# Pure (no DOM), so tests run it under node on its own.
PENDING_SCRIPT = """
/* drop the oldest pending entry matching an inbound frame's text; returns
   whether one matched (a frame from another tab matches nothing) */
function settlePending(list, text){
  const i = list.findIndex((p) => p.text === text);
  if (i < 0) return false;
  list.splice(i, 1); return true;
}

/* drop entries history already shows — the backstop for an inbound frame this
   tab never saw (sent, then switched away; an SSE outage). Each entry noted how
   many owner rows carried its text when sent, so a higher count means it
   landed; identical texts settle oldest-first, one per extra row. */
function reconcilePending(list, ownerTexts){
  const count = {}, used = {};
  ownerTexts.forEach((t) => { count[t] = (count[t] || 0) + 1; });
  for (let i = 0; i < list.length; i++){
    const p = list[i], landed = (count[p.text] || 0) - p.seen;
    if (landed > (used[p.text] || 0)){
      used[p.text] = (used[p.text] || 0) + 1; list.splice(i--, 1);
    }
  }
}
"""

SEND_SCRIPT = PENDING_SCRIPT + """
const pending = {};  /* thread -> [{text, mode, seen}] sent, not yet taken */
const TOUCH = matchMedia("(pointer: coarse)").matches;
const PENDING_TAG = {queued: "queued", injected: "interrupt \\u2014 next step"};
input.placeholder = "message chief   \\u00b7   / for commands   \\u00b7   " +
  (TOUCH ? "enter interrupts" : "ctrl+enter interrupts");

function renderPending(){
  const box = el("queued"); box.replaceChildren();
  (pending[state.current] || []).forEach((p) => {
    const d = document.createElement("div"); d.className = "msg owner pending";
    d.append(span("who"), span(null, p.text), span("ptag", PENDING_TAG[p.mode]));
    box.appendChild(d);
  });
}

function ownerRows(text){
  return [...log.querySelectorAll(".msg.owner")]
    .filter((d) => d.lastChild.textContent === text).length;
}

/* an inbound frame: the turn took the text, so it moves into the transcript */
function onInbound(thread, text){
  if (settlePending(pending[thread] || [], text)) renderPending();
  addMsg("owner", text);
}

async function send(interrupt){
  if (acceptCompletion()) return;
  const text = input.value.trim(); if (!text || input.disabled) return;
  const tk = state.current;
  const s = state.sessions.find((x) => x.thread === tk);
  if (s && s.send_guard &&
      !confirm(`This reply goes to ${label(tk)} on ${s.channel} — not you. Send it?`))
    return;
  const item = {text, mode: interrupt ? "injected" : "queued", seen: ownerRows(text)};
  (pending[tk] ||= []).push(item); renderPending();
  input.value = ""; hidePop();
  let outcome = null;
  try {
    const r = await fetch("/send", {method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({thread: tk, text, interrupt})});
    if (r.ok) outcome = await r.text();
  } catch (err) { /* network down: treated like a refusal below */ }
  if (outcome === "injected" || outcome === "queued"){
    item.mode = outcome;           /* an idle thread turns an interrupt into a turn */
  } else {
    const q = pending[tk] || [], i = q.indexOf(item);
    if (i >= 0) q.splice(i, 1);
    /* a card answer or a command: shown, but never streamed back verbatim */
    if (outcome && tk === state.current) addMsg("owner", text);
    if (!outcome && !input.value) input.value = text;  /* failed: keep the draft */
  }
  if (tk === state.current) renderPending();
}

el("f").onsubmit = (e) => { e.preventDefault(); send(TOUCH); };
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)){
    e.preventDefault(); send(true);
  }
});
"""
