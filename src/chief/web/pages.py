"""HTML shells for the web UI. Server-rendered; CSS/JS served from /app.*."""

LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>chief</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:dark}
body{background:#1d2021;color:#ebdbb2;margin:0;height:100vh;
  font:15px/1.5 ui-monospace,"DejaVu Sans Mono","SFMono-Regular",Menlo,monospace;
  display:grid;place-items:center}
form{display:flex;gap:.5rem;align-items:center}
.sigil{color:#fe8019}
input,button{font:inherit;padding:.5rem .7rem;background:#282828;color:#ebdbb2;
  border:1px solid #3c3836;border-radius:2px}
button{color:#1d2021;background:#fe8019;border-color:#fe8019;cursor:pointer}
.err{color:#fb4934;margin-left:.5rem}
</style></head><body>
<form method="post" action="/login">
  <span class="sigil">chief login&gt;</span>
  <input type="password" name="password" placeholder="owner password" autofocus>
  <button>enter</button>{error}
</form>
</body></html>"""

CHAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>chief</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/app.css">
</head><body>
<header id="statusbar">
  <span class="seg brand">chief</span>
  <span class="seg" id="conn"><i class="dot"></i
    ><span id="conn-label">connecting</span></span>
  <span class="seg buf" id="buf-seg">&mdash;</span>
  <span class="seg" id="model-seg">model &mdash;</span>
  <span class="seg grow"></span>
  <span class="seg" id="mon-seg">mon &hellip;</span>
</header>
<div id="main">
  <nav id="buffers">
    <div class="panel-label">buffers</div>
    <ul id="buflist"></ul>
    <button id="newbuf">+ new</button>
  </nav>
  <section id="pane">
    <div id="log"></div>
    <div id="readonly" hidden>attached read-only &mdash; this conversation
      lives on another channel.</div>
    <form id="f" autocomplete="off">
      <span class="sigil" id="sigil">owner&gt;</span>
      <input id="input" name="text"
        placeholder="message chief   &middot;   / for commands" autofocus>
      <ul id="complete" hidden></ul>
    </form>
  </section>
</div>
<script src="/app.js"></script>
</body></html>"""
