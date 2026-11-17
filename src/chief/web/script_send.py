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

# Pure too: the composer keybinds as a decision, so they test without a DOM.
KEYS_SCRIPT = """
/* a composer keydown -> "send" | "interrupt" | null (null: the textarea's own
   behaviour, so Shift+Enter inserts a newline and an IME keeps its Enter) */
function composerKey(e, touch){
  if (e.key !== "Enter" || e.isComposing) return null;
  if (e.ctrlKey || e.metaKey) return "interrupt";
  if (e.shiftKey || e.altKey) return null;
  return touch ? "interrupt" : "send";
}
"""

SEND_SCRIPT = PENDING_SCRIPT + KEYS_SCRIPT + """
const pending = {};  /* thread -> [{text, mode, seen}] sent, not yet taken */
const TOUCH = matchMedia("(pointer: coarse)").matches;
const PENDING_TAG = {queued: "queued", injected: "interrupt \\u2014 next step"};
input.placeholder = matchMedia("(max-width: 640px)").matches
  ? "message chief \\u00b7 / for commands"
  : "message chief   \\u00b7   / for commands   \\u00b7   " +
    (TOUCH ? "enter interrupts"
      : "shift+enter newline   \\u00b7   ctrl+enter interrupts");

/* grow the textarea with its text, up to a cap; past that it scrolls */
function fitInput(){
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + "px";
}
input.addEventListener("input", fitInput);

function renderPending(){
  const box = el("queued"); box.replaceChildren();
  (pending[state.current] || []).forEach((p) => {
    const d = document.createElement("div"); d.className = "msg owner pending";
    d.append(span("who"), span(null, p.text), span("ptag", PENDING_TAG[p.mode]));
    box.appendChild(d);
  });
}

/* stored owner rows only: reconcilePending compares against stored history */
function ownerRows(text){
  return [...log.querySelectorAll(".msg.owner:not(.eph)")]
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
      !confirm(`This thread reaches ${label(tk)} on ${s.channel} — not you. `
        + `Chief's reply stays here in the cockpit; nothing is sent to them `
        + `unless chief uses a gated tool. Send it?`))
    return;
  /* a slash command is answered at once and never stored: show it now (its
     answer can beat this request's reply) as a client-only row */
  const cmd = text.startsWith("/") ? addEph("owner", text) : null;
  if (cmd) (sentCmds[tk] ||= []).push(cmd);
  const item = cmd ? null
    : {text, mode: interrupt ? "injected" : "queued", seen: ownerRows(text)};
  if (item){ (pending[tk] ||= []).push(item); renderPending(); }
  input.value = ""; fitInput(); hidePop();
  let outcome = null, reason = "network error";
  try {
    const r = await fetch("/send", {method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({thread: tk, text, interrupt})});
    if (r.ok) outcome = await r.text(); else reason = await r.text();
  } catch (err) { /* network down: treated like a refusal below */ }
  if (item && (outcome === "injected" || outcome === "queued")){
    item.mode = outcome;           /* an idle thread turns an interrupt into a turn */
  } else {
    const q = pending[tk] || [], i = q.indexOf(item);
    if (i >= 0) q.splice(i, 1);
    if (cmd && outcome !== "command"){             /* no answer coming */
      const w = sentCmds[tk] || [], j = w.indexOf(cmd);
      if (j >= 0) w.splice(j, 1);
    }
    /* a card answer: shown, but never streamed back or stored */
    if (outcome && !cmd && tk === state.current) addEph("owner", text);
    if (!outcome){                              /* failed: keep the draft */
      if (cmd) dropEph(tk, cmd);
      if (!input.value){ input.value = text; fitInput(); }
      notice("not sent \\u2014 " + (reason || "server error"), "bad");
    }
  }
  if (tk === state.current) renderPending();
}

el("f").onsubmit = (e) => { e.preventDefault(); send(TOUCH); };
input.addEventListener("keydown", (e) => {
  const act = composerKey(e, TOUCH); if (!act) return;
  e.preventDefault(); send(act === "interrupt");
});
"""
