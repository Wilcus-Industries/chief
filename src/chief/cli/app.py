"""The Textual TUI: render client-plane frames and turn keystrokes into wire frames.

One active CLI thread at a time (``/new`` switches it); an approval card raised on
*any* platform is still shown and answerable here (#136 broadcasts every card to every
client). ``ChiefCliApp.transcript`` is the test seam — every rendered line is recorded
there via :meth:`_write`, so tests never have to introspect the ``RichLog`` widget.
"""

from collections import deque
from typing import Any, NamedTuple, cast
from uuid import uuid4

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog, Static

from ..adapters.commands import OWNER_COMMANDS
from ..client_plane import (
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    PROTOCOL_VERSION,
    TYPE_BACKFILL,
    TYPE_CARD,
    TYPE_CARD_RESOLVED,
    TYPE_ERROR,
    TYPE_FILE,
    TYPE_HELLO,
    TYPE_MILESTONE,
    TYPE_PONG,
    TYPE_REPLY,
    TYPE_SKILLS_LIST,
    TYPE_STATUS_SNAPSHOT,
    TYPE_THREADS,
    answer_frame,
    command_frame,
    inject_frame,
    list_threads_frame,
    skills_frame,
    status_frame,
    switch_frame,
    user_frame,
)
from .connection import SocketConnection

#: Bands a run of replayed (#132 detach-replay) frames so the owner can tell missed
#: history from live traffic at a glance.
AWAY_HEADER = "── while you were away ──"
AWAY_FOOTER = "── caught up ──"

#: Slash commands this client handles itself, never forwarded to the daemon.
CLIENT_COMMANDS = (
    "/help", "/new", "/quit", "/tasks", "/switch", "/status", "/skills", "/drive",
)

#: Shown when the owner tries to send input into a foreign-platform pane (#138
#: finding) — that would dispatch through the ``platform=cli`` engine under the
#: foreign thread_key, spawning a spurious cli task the owner never sees answered.
#: ``/drive`` is the sanctioned way to act on such a pane (#135's cross-stack drive).
READ_ONLY_PANE_MESSAGE = (
    "read-only pane — /drive <text> to run a turn there, or /new to come back"
)


def _format_backfill_line(m: dict[str, object]) -> str:
    """Render one #132 history row the way a switched-to pane replays it (#138)."""
    if m.get("role") == "owner":
        return f"> {m.get('text', '')}"
    if m.get("kind") == "milestone":
        return f"· {m.get('text', '')}"
    if m.get("kind") == "file":
        return f"[file] {m.get('filename')}"
    return str(m.get("text", ""))


class Line(NamedTuple):
    """One rendered transcript line: the test seam — no ``RichLog`` introspection."""

    style: str
    text: str


class ChiefCliApp(App[None]):
    """The terminal client of the client plane (#137)."""

    #: Textual OVERLAYS widgets docked to the same edge — it does not stack them.
    #: Docking both #statusbar and #prompt put them on the bottom row alongside
    #: Footer (which docks itself), painting over the input's last row so the bar
    #: rendered cut off. Let Footer own the bottom edge alone; the statusbar and
    #: prompt sit in normal flow above it, and the transcript (1fr) takes the slack.
    CSS = (
        "#transcript { height: 1fr; } "
        "#statusbar { height: 1; } "
        "#prompt { height: 3; }"
    )
    TITLE = "chief"

    def __init__(
        self, connection: SocketConnection, *, thread_key: str = DEFAULT_THREAD_KEY
    ) -> None:
        super().__init__()
        self._conn = connection
        self._thread_key = thread_key
        #: The active pane's platform (#138 ``/switch``) — ``self._thread_key`` becomes
        #: that thread's key regardless of which platform it came from.
        self._active_platform: str = CLI_PLATFORM
        #: The most recent ``/tasks`` listing, so ``/switch <n>`` can resolve an index.
        self._last_threads: list[dict[str, object]] = []
        #: Every rendered line, in order — the test seam.
        self.transcript: list[Line] = []
        #: Unanswered approval ids, oldest first (FIFO — one prompt at a time).
        self._pending: deque[int] = deque()
        self._in_replay = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="transcript", wrap=True, markup=False)
        yield Static(id="statusbar")
        yield Input(placeholder="message chief — /help", id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one(Input).focus()
        self.run_worker(self._pump(), exclusive=True)
        await self._conn.send(status_frame())

    async def _pump(self) -> None:
        async for frame in self._conn.frames():
            try:
                self._render(frame)
            except (KeyError, ValueError, TypeError):
                self._write(f"! malformed frame: {frame.get('type', '?')}", "red")
        self._write("— disconnected —", "red")

    async def on_unmount(self) -> None:
        await self._conn.close()

    def _write(self, text: str, style: str = "") -> None:
        self.transcript.append(Line(style, text))
        self.query_one("#transcript", RichLog).write(Text(text, style=style))

    def _render(self, frame: dict[str, object]) -> None:
        ftype = frame.get("type")
        if ftype == TYPE_HELLO:
            if frame.get("protocol") != PROTOCOL_VERSION:
                self._write(
                    f"! protocol mismatch: daemon {frame.get('protocol')},"
                    f" client {PROTOCOL_VERSION}",
                    "red",
                )
            return
        if ftype == TYPE_PONG:
            return
        if ftype == TYPE_ERROR:
            self._write(f"! {frame.get('code')}: {frame.get('message')}", "red")
            return
        if ftype == TYPE_THREADS:
            self._render_threads(frame)
            return
        if ftype == TYPE_STATUS_SNAPSHOT:
            self._render_status(frame)
            return
        if ftype == TYPE_SKILLS_LIST:
            self._render_skills(frame)
            return
        if ftype == TYPE_BACKFILL:
            self._render_backfill(frame)
            return

        scope = self._scope(frame)
        if scope == "hidden":
            return
        if scope == "notice":
            self._write(
                f"🔔 activity in ({frame.get('platform')}) {frame.get('thread_key')}",
                "dim",
            )
            return

        replay = bool(frame.get("replay"))
        if replay and not self._in_replay:
            self._write(AWAY_HEADER, "dim")
            self._in_replay = True
        elif not replay and self._in_replay:
            self._write(AWAY_FOOTER, "dim")
            self._in_replay = False

        tag = "" if frame.get("platform") == CLI_PLATFORM else f"({frame['platform']}) "
        if ftype == TYPE_REPLY:
            self._write(f"{tag}{frame.get('text')}")
        elif ftype == TYPE_MILESTONE:
            self._write(f"· {tag}{frame.get('text')}", "dim")
        elif ftype == TYPE_FILE:
            caption = frame.get("caption")
            suffix = f" — {caption}" if caption else ""
            self._write(f"[file] {frame.get('filename')}{suffix}", "italic")
        elif ftype == TYPE_CARD:
            approval_id = frame["approval_id"]
            if not isinstance(approval_id, int):
                raise ValueError("card approval_id must be an int")
            self._write(
                f"{tag}[approval #{approval_id}] {frame.get('text')}", "bold yellow"
            )
            self._pending.append(approval_id)
            self._prompt_card()
        elif ftype == TYPE_CARD_RESOLVED:
            approval_id = frame["approval_id"]
            if not isinstance(approval_id, int):
                raise ValueError("card_resolved approval_id must be an int")
            self._write(f"{tag}[approval #{approval_id}] {frame.get('text')}", "dim")
            if approval_id in self._pending:
                self._pending.remove(approval_id)
                self._end_card_prompt()
                self._prompt_card()

    def _scope(self, frame: dict[str, object]) -> str:
        """Where a live frame lands: full render, a one-line background notice, or
        nowhere (#138). Cards are always full (any client may answer any approval,
        #136); a CLI-platform replay is always full regardless of active thread (missed
        history bands together, #132); everything else is full only when it matches the
        active ``(platform, thread_key)`` pane — otherwise it's a background notice so a
        busy background thread is never silently invisible.
        """
        ftype = frame.get("type")
        if ftype in (TYPE_CARD, TYPE_CARD_RESOLVED):
            return "full"
        if bool(frame.get("replay")) and frame.get("platform") == CLI_PLATFORM:
            return "full"
        if (
            frame.get("platform") == self._active_platform
            and frame.get("thread_key") == self._thread_key
        ):
            return "full"
        if ftype in (TYPE_REPLY, TYPE_MILESTONE, TYPE_FILE):
            return "notice"
        return "hidden"

    def _render_threads(self, frame: dict[str, object]) -> None:
        threads = cast(list[dict[str, object]], frame.get("threads", []))
        self._last_threads = list(threads)
        if not threads:
            self._write("no active threads", "dim")
            return
        for i, t in enumerate(threads, start=1):
            self._write(
                f"{i}. ({t['platform']}) {t['thread_key']} — "
                f"{t.get('title') or t['thread_key']} [{t['status']}]"
            )

    def _render_backfill(self, frame: dict[str, object]) -> None:
        self._active_platform = str(frame["platform"])
        self._thread_key = str(frame["thread_key"])
        self.query_one("#transcript", RichLog).clear()
        self.transcript.clear()
        self._write(
            f"── switched to ({self._active_platform}) {self._thread_key} ──", "dim"
        )
        messages = cast(list[dict[str, object]], frame.get("messages", []))
        for m in messages:
            self._write(
                _format_backfill_line(m), "dim" if m.get("kind") == "milestone" else ""
            )

    def _render_status(self, frame: dict[str, object]) -> None:
        tasks = cast(list[dict[str, object]], frame.get("tasks", []))
        budget = cast(list[dict[str, object]], frame.get("budget", []))
        schedules = cast(list[dict[str, object]], frame.get("schedules", []))
        self._write("── status ──", "bold")
        if not tasks:
            self._write("no active tasks", "dim")
        for t in tasks:
            model = t.get("model") or "default"
            self._write(
                f"• ({t['platform']}) {t['thread_key']} — {t['status']} [{model}]"
            )
        for b in budget:
            self._write(
                f"spend: {b['currency']} {b['spent']:.2f}/{b['cap']:.2f} ({b['mode']})",
                "dim",
            )
        if schedules:
            self._write("upcoming:", "dim")
            for s in schedules:
                self._write(
                    f"  {s['next_run']} — {s['kind']} {s['action_type']}", "dim"
                )
        self._update_statusbar(frame)

    def _update_statusbar(self, frame: dict[str, object]) -> None:
        tasks = cast(list[dict[str, object]], frame.get("tasks", []))
        budget = cast(list[dict[str, object]], frame.get("budget", []))
        mine = next(
            (
                t
                for t in tasks
                if t.get("platform") == self._active_platform
                and t.get("thread_key") == self._thread_key
            ),
            None,
        )
        model = (mine.get("model") if mine else None) or "default"
        spend = ", ".join(
            f"{b['currency']}={b['spent']:.0f}/{b['cap']:.0f}" for b in budget
        ) or "—"
        self.query_one("#statusbar", Static).update(
            f"model: {model} | spend: {spend} | active tasks: {len(tasks)}"
        )

    def _render_skills(self, frame: dict[str, object]) -> None:
        skills = cast(list[Any], frame.get("skills", []))
        if not skills:
            self._write("no skills composed", "dim")
            return
        self._write("skills: " + ", ".join(str(s) for s in skills))

    def _prompt_card(self) -> None:
        if not self._pending:
            return
        inp = self.query_one(Input)
        if inp.disabled:
            return  # a prompt is already active
        self._write("approve? [y/n]", "bold")
        inp.disabled = True
        self.set_focus(None)

    def _end_card_prompt(self) -> None:
        inp = self.query_one(Input)
        inp.disabled = False
        self.set_focus(inp)

    async def on_key(self, event: events.Key) -> None:
        if not self._pending or event.key not in ("y", "n"):
            return
        event.stop()
        approval_id = self._pending.popleft()
        action = "approve_once" if event.key == "y" else "deny_once"
        await self._conn.send(answer_frame(approval_id, action))
        self._write(f"[approval #{approval_id}] {action}", "dim")
        self._end_card_prompt()
        self._prompt_card()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        if self._active_platform != CLI_PLATFORM:
            self._write(f"! {READ_ONLY_PANE_MESSAGE}", "red")
            return
        self._write(f"> {text}", "bold")
        await self._conn.send(user_frame(self._thread_key, text))

    async def _handle_slash(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        name, arg = name.strip(), arg.strip()
        if not name:
            self._write("! empty command", "red")
            return
        if name == "help":
            forwarded = [
                n for n in OWNER_COMMANDS.names() if f"/{n}" not in CLIENT_COMMANDS
            ]
            self._write(", ".join(CLIENT_COMMANDS) + " — client commands", "dim")
            self._write(
                ", ".join("/" + n for n in forwarded) + " — forwarded to the daemon",
                "dim",
            )
            return
        if name == "new":
            self._active_platform = CLI_PLATFORM
            self._thread_key = f"cli:{uuid4().hex[:8]}"
            self.query_one("#transcript", RichLog).clear()
            self.transcript.clear()
            self._write(f"new thread: {self._thread_key}", "dim")
            return
        if name == "quit":
            await self._conn.close()
            self.exit()
            return
        if name == "tasks":
            await self._conn.send(list_threads_frame())
            return
        if name == "switch":
            await self._cmd_switch(arg)
            return
        if name == "status":
            await self._conn.send(status_frame())
            return
        if name == "skills":
            await self._conn.send(skills_frame())
            return
        if name == "drive":
            await self._cmd_drive(arg)
            return
        if self._active_platform != CLI_PLATFORM:
            self._write(f"! {READ_ONLY_PANE_MESSAGE}", "red")
            return
        await self._conn.send(command_frame(self._thread_key, name, arg))

    async def _cmd_drive(self, text: str) -> None:
        """Cross-stack drive (#135): run ``text`` as a real owner turn on the *foreign*
        thread this pane is switched onto.

        The daemon echoes it into that platform's real chat marked as via-CLI and
        dispatches on that platform's engine, so the answer arrives where the
        conversation lives — on the phone, not here. The pane stays read-only: a drive
        is one turn, not an attach. This is the only client path to an ``inject`` frame;
        without it #135 ships with nothing able to send one.
        """
        if self._active_platform == CLI_PLATFORM:
            self._write(
                "! /drive acts on another platform's thread — /tasks, then /switch <n>",
                "red",
            )
            return
        if not text:
            self._write("! usage: /drive <text>", "red")
            return
        self._write(f"> [via CLI → {self._active_platform}] {text}", "bold")
        await self._conn.send(
            inject_frame(self._active_platform, self._thread_key, text)
        )

    async def _cmd_switch(self, arg: str) -> None:
        if not arg.isdigit() or not self._last_threads:
            self._write("! usage: /switch <n> — run /tasks first", "red")
            return
        idx = int(arg) - 1
        if not (0 <= idx < len(self._last_threads)):
            self._write(f"! no thread #{arg} — run /tasks", "red")
            return
        t = self._last_threads[idx]
        await self._conn.send(switch_frame(str(t["platform"]), str(t["thread_key"])))
