"""JS for the transcript's activity groups and approval cards.

An activity group is one run of tool calls and thinking with no reply text
between them, collapsed by default to a single row naming the newest call.
Expanded, it lists each step; a tool step's args + result are fetched lazily
off its own expand (``/history/tool``), and what the owner opened stays open
across re-renders. An approval card renders yes/always/no buttons that POST
/approve, answering the same broker the origin channel's reply would (#267).
Split out of script.py to keep that file under the length cap (#261, #263);
all JS is concatenated into one global scope.
"""

TOOL_SCRIPT = """
/* approval card: mirrors a gray-zone tool call's card to this dashboard —
   answering here resolves the same broker an origin-channel reply would */
function addApproval(thread, question){
  dropEmpty();
  const d = document.createElement("div"); d.className = "msg approval";
  d.dataset.thread = thread;
  const btns = document.createElement("div"); btns.className = "approval-btns";
  [["yes", "approve"], ["always", "always allow"], ["no", "deny"]].forEach(
    ([answer, label]) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label;
      b.onclick = async () => {
        [...btns.children].forEach((x) => x.disabled = true);
        await fetch("/approve", {method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({thread, answer})});
      };
      btns.appendChild(b);
    }
  );
  d.append(span("who", "approval"), span(null, question), btns);
  log.appendChild(d); follow();
  /* the server takes the next message as this card's answer, not a turn */
  if (!input.dataset.ph) input.dataset.ph = input.placeholder;
  input.placeholder = "typing here answers the approval card";
}

function resolveApproval(thread){
  const card = log.querySelector(`.approval[data-thread="${CSS.escape(thread)}"]`);
  if (card) card.remove();
  if (input.dataset.ph){
    input.placeholder = input.dataset.ph; delete input.dataset.ph;
  }
}

/* ---- activity groups ---- */
const openCalls = new Set();   /* call ids the owner expanded */
const openGroups = new Set();  /* groups left open, keyed by their first call id */
const callCache = {};          /* call id -> a settled /history/tool answer */

const details = (cls, label) => {
  const d = document.createElement("details"); d.className = cls;
  const s = document.createElement("summary"); s.textContent = label;
  d.appendChild(s); return d;
};
const pre = (cls, text) => {
  const p = document.createElement("pre"); p.className = cls;
  p.textContent = text; return p;
};

/* the open group at the transcript's tail, or a new one */
function activityGroup(){
  const last = log.lastElementChild;
  if (last && last.classList.contains("activity")) return last;
  dropEmpty();
  const g = details("msg activity solo", "");
  const list = document.createElement("div"); list.className = "act-list";
  g.appendChild(list); g.tools = []; g.key = null;
  g.ontoggle = () => {
    if (g.key) g.open ? openGroups.add(g.key) : openGroups.delete(g.key);
  };
  log.appendChild(g); return g;
}

/* thinking alone shows as its own collapsed row (the group is held open, its
   header hidden); once a tool call joins, the group collapses to one row */
function relabel(g){
  const solo = !g.tools.length;
  if (solo) g.open = true;
  else if (g.classList.contains("solo")) g.open = !!g.key && openGroups.has(g.key);
  g.classList.toggle("solo", solo);
  g.firstChild.textContent = groupLabel(g.tools);
}

/* one tool step: name only; args + result load on its expand */
function addTool(callId, name){
  const g = activityGroup(), d = details("act tool", name);
  const body = document.createElement("div"); body.className = "act-body";
  d.appendChild(body); g.tools.push(name);
  if (!g.key && callId) g.key = callId;
  if (!callId){             /* results=off: the live view withholds the handle */
    d.classList.add("noresult"); d.firstChild.title = "result hidden by stream policy";
    d.firstChild.onclick = (e) => e.preventDefault();
  } else {
    d.ontoggle = () => {
      if (!d.open){ openCalls.delete(callId); return; }
      openCalls.add(callId); loadCall(callId, body);
    };
    if (openCalls.has(callId)) d.open = true;   /* toggle fires: reloads it */
  }
  g.lastChild.appendChild(d); relabel(g); follow();
}

async function loadCall(callId, body){
  let r = callCache[callId];
  if (!r){
    body.replaceChildren(span("act-note", "loading\\u2026"));
    const t = encodeURIComponent(state.current), c = encodeURIComponent(callId);
    r = await getJSON(`/history/tool?thread=${t}&call_id=${c}`);
    if (r && r.status !== "pending") callCache[callId] = r;
  }
  if (!r || r.status === "pending"){
    body.replaceChildren(span("act-note", "result pending \\u2014 reopen to retry"));
  } else if (r.status !== "ok"){
    body.replaceChildren(span("act-note", "unavailable \\u2014 folded away by compaction"));
  } else {
    body.replaceChildren(pre("act-args", JSON.stringify(r.args, null, 2)),
      pre("act-result", r.result));
  }
}

/* one thinking step; returns its text node's holder for live deltas */
function addThinking(text){
  const g = activityGroup(), d = details("act thinking", "thinking");
  const body = document.createElement("div"); body.className = "think-text";
  body.textContent = text; d.appendChild(body);
  g.lastChild.appendChild(d); relabel(g); follow(); return body;
}
"""
