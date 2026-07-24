"""JS for the focused buffer's stream-policy panel (#265): shows the resolved
policy and its provenance (channel default vs a per-thread override), and
flips one field at a time — each change POSTs straight to /policy, no save
button, no page reload; the very next turn streams under it since dispatch
re-resolves the policy fresh every turn. Split out to keep script.py under
the length cap (#261, #263)."""

POLICY_SCRIPT = """
const polDeltas = el("pol-deltas"), polTools = el("pol-tools"),
  polResults = el("pol-results"), polGuard = el("pol-guard"),
  polSrc = el("pol-src"), polReset = el("pol-reset");

function renderPolicy(p, isOverride){
  polDeltas.checked = p.deltas; polTools.checked = p.tools;
  polResults.value = p.results; polGuard.checked = p.send_guard;
  polSrc.textContent = isOverride ? "override" : "default";
  polSrc.className = "psrc " + (isOverride ? "override" : "default");
  polReset.hidden = !isOverride;
}

async function loadPolicy(tk){
  const p = await getJSON("/policy?thread=" + encodeURIComponent(tk));
  if (!p || !p.resolved || tk !== state.current) return;
  renderPolicy(p.resolved, p.override != null);
}

async function savePolicy(policy){
  const tk = state.current;
  await fetch("/policy", {method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({thread: tk, policy})});
  loadPolicy(tk);
  // Refresh the cached send_guard so the send confirm honors the just-saved
  // policy — /sessions recomputes it server-side (guard_audience).
  loadSessions();
}

function currentPolicy(){
  return {deltas: polDeltas.checked, tools: polTools.checked,
    results: polResults.value, send_guard: polGuard.checked};
}

[polDeltas, polTools, polGuard, polResults].forEach(
  (i) => i.onchange = () => savePolicy(currentPolicy()));
polReset.onclick = () => savePolicy(null);
"""
