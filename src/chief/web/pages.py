"""HTML for the web UI. Server-rendered, zero build step, no external assets."""

LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>chief</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0}
form{display:flex;gap:.5rem}input,button{font-size:1rem;padding:.5rem}
.err{color:#b00}
</style></head><body>
<form method="post" action="/login">
  <input type="password" name="password" placeholder="owner password" autofocus>
  <button>enter</button>{error}
</form>
</body></html>"""

CHAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>chief</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui;margin:0;display:flex;flex-direction:column;height:100vh}
#log{flex:1;overflow-y:auto;padding:1rem;white-space:pre-wrap}
.msg{margin:.4rem 0;padding:.5rem .8rem;border-radius:.6rem;max-width:60rem}
.me{background:#dde8ff}.agent{background:#eee}
form{display:flex;gap:.5rem;padding:.8rem;border-top:1px solid #ccc}
input[name=text]{flex:1;font-size:1rem;padding:.5rem}
#side{font-size:.85rem;color:#555;padding:.3rem 1rem;border-top:1px solid #eee}
</style></head><body>
<div id="log"></div>
<div id="side">monitors: <span id="monitors">…</span></div>
<form id="f">
<input name="text" placeholder="message chief" autocomplete="off" autofocus>
<button>send</button></form>
<script>
const log = document.getElementById("log");
let live = null;
function add(cls, text){
  const d = document.createElement("div");
  d.className = "msg " + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
const es = new EventSource("/events");
es.onmessage = (e) => {
  const f = JSON.parse(e.data);
  if (f.type === "delta"){
    if (!live) live = add("agent", "");
    live.textContent += f.text;
  } else if (f.type === "final"){
    if (live){ live.textContent = f.text; live = null; }
    else add("agent", f.text);
  }
};
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const input = e.target.elements.text;
  if (!input.value.trim()) return;
  add("me", input.value);
  await fetch("/send", {method:"POST",
    headers:{"content-type":"application/json"},
    body: JSON.stringify({thread:"main", text: input.value})});
  input.value = "";
};
async function refreshMonitors(){
  const r = await fetch("/monitors");
  document.getElementById("monitors").textContent = await r.text();
}
refreshMonitors(); setInterval(refreshMonitors, 10000);
</script></body></html>"""
