"""JS for the transcript's tool-call rows and the live SSE stream client:
tool rows are collapsed by default (args + result fetched lazily off a click),
and the stream subscribes to the focused thread, renders its rich frames, and
re-syncs from history on a final frame or an SSE reconnect. Split out of
script.py to keep that file under the length cap (#261, #263). All JS is
concatenated into one global scope, so cross-file references resolve fine."""

TOOL_SCRIPT = """
/* collapsed tool-call row: name only, args + result fetched on demand */
function addTool(callId, name){
  const empty = log.querySelector(".empty"); if (empty) empty.remove();
  const d = document.createElement("div"); d.className = "msg tool";
  const btn = document.createElement("button");
  btn.className = "toolload"; btn.type = "button"; btn.textContent = "load result";
  btn.onclick = async () => {
    btn.disabled = true; btn.textContent = "loading\\u2026";
    const t = encodeURIComponent(state.current), c = encodeURIComponent(callId);
    const r = await getJSON(`/history/tool?thread=${t}&call_id=${c}`);
    if (!r || r.status === "pending"){
      btn.disabled = false; btn.textContent = "load result (pending)"; return;
    }
    d.append(span("toolbody", r.status === "ok"
      ? `args: ${JSON.stringify(r.args)}\\nresult: ${r.result}`
      : "[unavailable \\u2014 folded away by compaction]"));
    btn.remove();
  };
  d.append(span("who", `tool\\u2009>\\u2009${name}`), btn); log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

/* ---- live stream ---- */
let es = null, wasDown = false;

function bumpThread(thread, preview){
  if (preview != null) snippets[thread] = preview;
  const i = state.sessions.findIndex((s) => s.thread === thread);
  if (i < 0){ loadSessions(); return; }            /* unknown thread: refetch */
  state.sessions.unshift(state.sessions.splice(i, 1)[0]);  /* newest-first */
  if (thread !== state.current) unread.add(thread);
  renderBuffers();
}

/* canonical re-render of a thread from stored history */
async function reloadHistory(tk){
  const rows = await getJSON("/history?thread=" + encodeURIComponent(tk));
  if (tk !== state.current) return;                /* switched away mid-fetch */
  live = null; log.replaceChildren();
  if (!rows || !rows.length){
    log.appendChild(span("empty", "no messages yet \\u2014 type below to start."));
  } else {
    rows.forEach((r) =>
      r.role === "tool" ? addTool(r.call_id, r.name) : addMsg(r.role, r.text));
  }
  log.scrollTop = log.scrollHeight;
}

function connect(tk){
  es = new EventSource("/events?thread=" + encodeURIComponent(tk));
  const conn = el("conn"), lbl = el("conn-label");
  es.onopen = () => {
    conn.className = "seg live"; lbl.textContent = "live";
    if (wasDown){ wasDown = false; reloadHistory(state.current); }
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
  if (f.type === "inbound") addMsg("owner", f.text);
  else if (f.type === "tool") addTool(f.call_id, f.name);
  else if (f.type === "delta"){
    if (!live) live = addMsg("chief", ""); live.textContent += f.text;
  } else if (f.type === "final"){
    if (live){ live.textContent = f.text; live = null; }
    else addMsg("chief", f.text);
    bumpThread(f.thread, f.text); reloadHistory(f.thread);
  }
  log.scrollTop = log.scrollHeight;
}
"""
