/* chief.js — the chat page's hand-written client (no framework, no build step).
 *
 * Owns the /events SSE stream (server-rendered HTML fragments for messages and
 * approvals; JSON for the ephemeral delta/tool events), the streaming reply
 * bubble, the live tool chips, and the composer's slash-command autocomplete.
 * Everything else on the page stays htmx.
 *
 * Trust boundary: every fragment injected via insertAdjacentHTML/innerHTML is
 * rendered and HTML-escaped by our own server (chief.web.render) and delivered
 * over the authenticated /events stream — the same model the htmx SSE extension
 * had. Everything user- or model-authored that this file itself renders (delta
 * text, tool names, command labels) goes through textContent, never markup.
 */
(function () {
  "use strict";

  var chat = document.querySelector(".chat[data-events-url]");
  if (!chat) return;
  var transcript = document.getElementById("transcript");
  var approvals = document.getElementById("approvals");

  function scrollToEnd() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  /* ---- live stream ------------------------------------------------------ */

  var bubbles = {}; // message_id -> streaming bubble element
  var chips = {}; // tool_call_id -> running chip element

  function onMessage(e) {
    transcript.insertAdjacentHTML("beforeend", e.data);
    scrollToEnd();
  }

  function onApprovals(e) {
    approvals.innerHTML = e.data;
    if (window.htmx) window.htmx.process(approvals);
  }

  function onDelta(e) {
    var d = JSON.parse(e.data);
    var el = bubbles[d.message_id];
    if (d.done) {
      // The full block follows as a normal message event; retire the draft.
      if (el) el.remove();
      delete bubbles[d.message_id];
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.className = "msg chief streaming";
      el.appendChild(document.createTextNode(""));
      transcript.appendChild(el);
      bubbles[d.message_id] = el;
    }
    el.firstChild.textContent += d.text;
    scrollToEnd();
  }

  function onTool(e) {
    var t = JSON.parse(e.data);
    if (t.status === "start") {
      var chip = document.createElement("div");
      chip.className = "tool running";
      var name = document.createElement("span");
      name.className = "tool-name";
      name.textContent = t.name;
      chip.appendChild(name);
      transcript.appendChild(chip);
      chips[t.tool_call_id] = chip;
      scrollToEnd();
      return;
    }
    var open = chips[t.tool_call_id];
    if (!open) return;
    open.classList.remove("running");
    open.classList.add(t.ok ? "ok" : "fail");
    if (!t.ok && t.detail) {
      var detail = document.createElement("span");
      detail.className = "tool-detail";
      detail.textContent = t.detail;
      open.appendChild(detail);
    }
    delete chips[t.tool_call_id];
  }

  var source = new EventSource(chat.dataset.eventsUrl);
  source.addEventListener("message", onMessage);
  source.addEventListener("approvals", onApprovals);
  source.addEventListener("delta", onDelta);
  source.addEventListener("tool", onTool);

  scrollToEnd();

  /* ---- slash-command autocomplete --------------------------------------- */

  var input = document.querySelector('.composer input[name="text"]');
  var form = document.querySelector("form.composer");
  var menu = document.getElementById("cmd-menu");
  var dataEl = document.getElementById("cmd-data");
  if (!input || !menu || !dataEl) return;
  var commands = JSON.parse(dataEl.textContent || "[]");

  var open = false;
  var matches = [];
  var selected = 0;

  function hideMenu() {
    open = false;
    menu.hidden = true;
    menu.textContent = "";
  }

  function renderMenu() {
    menu.textContent = "";
    matches.forEach(function (cmd, i) {
      var row = document.createElement("div");
      row.className = "cmd-item" + (i === selected ? " selected" : "");
      row.setAttribute("role", "option");
      var name = document.createElement("span");
      name.className = "cmd-name";
      name.textContent = cmd.name;
      var desc = document.createElement("span");
      desc.className = "cmd-desc";
      desc.textContent = cmd.desc;
      row.appendChild(name);
      row.appendChild(desc);
      row.addEventListener("pointerdown", function (ev) {
        ev.preventDefault(); // keep focus in the input
        accept(i);
      });
      menu.appendChild(row);
    });
    menu.hidden = matches.length === 0;
    open = matches.length > 0;
  }

  function refresh() {
    var value = input.value;
    if (value.charAt(0) === "/" && value.indexOf(" ") === -1) {
      matches = commands.filter(function (cmd) {
        return cmd.name.indexOf(value) === 0;
      });
      selected = Math.min(selected, Math.max(matches.length - 1, 0));
      renderMenu();
    } else if (open) {
      hideMenu();
    }
  }

  function accept(i) {
    var chosen = matches[i];
    if (!chosen) return;
    input.value = chosen.name;
    hideMenu();
    form.requestSubmit();
  }

  input.addEventListener("input", function () {
    selected = 0;
    refresh();
  });

  input.addEventListener("keydown", function (ev) {
    if (!open) return;
    if (ev.key === "ArrowDown") {
      selected = (selected + 1) % matches.length;
      renderMenu();
    } else if (ev.key === "ArrowUp") {
      selected = (selected - 1 + matches.length) % matches.length;
      renderMenu();
    } else if (ev.key === "Escape") {
      hideMenu();
    } else if (ev.key === "Tab") {
      var chosen = matches[selected];
      if (chosen) {
        input.value = chosen.name;
        input.setSelectionRange(chosen.name.length, chosen.name.length);
        refresh();
      }
    } else if (ev.key === "Enter") {
      accept(selected);
    } else {
      return;
    }
    ev.preventDefault();
  });
})();
