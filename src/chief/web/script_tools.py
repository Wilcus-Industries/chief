"""JS for the transcript's tool-call rows: collapsed by default, args and
result fetched lazily off a click. Split out of script.py to keep that file
under the length cap — this is its own concern (#261)."""

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
"""
