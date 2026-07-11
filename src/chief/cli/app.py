"""The Textual TUI: render client-plane frames and turn keystrokes into wire frames.

One active CLI thread at a time (``/new`` switches it); an approval card raised on
*any* platform is still shown and answerable here (#136 broadcasts every card to every
client). ``ChiefCliApp.transcript`` is the test seam — every rendered line is recorded
there via :meth:`_write`, so tests never have to introspect the ``RichLog`` widget.
"""

from collections import deque
from typing import NamedTuple
from uuid import uuid4

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog

from ..adapters.commands import OWNER_COMMANDS
from ..client_plane import (
    CLI_PLATFORM,
    DEFAULT_THREAD_KEY,
    PROTOCOL_VERSION,
    TYPE_CARD,
    TYPE_CARD_RESOLVED,
    TYPE_ERROR,
    TYPE_FILE,
    TYPE_HELLO,
    TYPE_MILESTONE,
    TYPE_PONG,
    TYPE_REPLY,
    answer_frame,
    command_frame,
    user_frame,
)
from .connection import SocketConnection

#: Bands a run of replayed (#132 detach-replay) frames so the owner can tell missed
#: history from live traffic at a glance.
AWAY_HEADER = "── while you were away ──"
AWAY_FOOTER = "── caught up ──"

#: Slash commands this client handles itself, never forwarded to the daemon.
CLIENT_COMMANDS = ("/help", "/new", "/quit")


class Line(NamedTuple):
    """One rendered transcript line: the test seam — no ``RichLog`` introspection."""

    style: str
    text: str


class ChiefCliApp(App[None]):
    """The terminal client of the client plane (#137)."""

    CSS = "#transcript { height: 1fr; } #prompt { dock: bottom; }"
    TITLE = "chief"

    def __init__(
        self, connection: SocketConnection, *, thread_key: str = DEFAULT_THREAD_KEY
    ) -> None:
        super().__init__()
        self._conn = connection
        self._thread_key = thread_key
        #: Every rendered line, in order — the test seam.
        self.transcript: list[Line] = []
        #: Unanswered approval ids, oldest first (FIFO — one prompt at a time).
        self._pending: deque[int] = deque()
        self._in_replay = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="transcript", wrap=True, markup=False)
        yield Input(placeholder="message chief — /help", id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one(Input).focus()
        self.run_worker(self._pump(), exclusive=True)

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
        if not self._visible(frame):
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

    def _visible(self, frame: dict[str, object]) -> bool:
        """True iff this frame belongs on this client's transcript.

        A card/card_resolved is broadcast to every platform (#136 — any client may
        answer any approval), so it is always visible. Everything else is only ours if
        it came off the CLI platform, and then only for our thread key — unless it is a
        replay, where every missed CLI frame belongs in the away section regardless of
        which thread raised it.
        """
        ftype = frame.get("type")
        if ftype == TYPE_CARD or ftype == TYPE_CARD_RESOLVED:
            return True
        if frame.get("platform") != CLI_PLATFORM:
            return False
        if bool(frame.get("replay")):
            return True
        return frame.get("thread_key") == self._thread_key

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
        self._write(f"> {text}", "bold")
        await self._conn.send(user_frame(self._thread_key, text))

    async def _handle_slash(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        name, arg = name.strip(), arg.strip()
        if not name:
            self._write("! empty command", "red")
            return
        if name == "help":
            owner_names = ", ".join("/" + n for n in OWNER_COMMANDS.names())
            self._write(", ".join(CLIENT_COMMANDS) + " — client commands", "dim")
            self._write(owner_names + " — forwarded to the daemon", "dim")
            return
        if name == "new":
            self._thread_key = f"cli:{uuid4().hex[:8]}"
            self.query_one("#transcript", RichLog).clear()
            self.transcript.clear()
            self._write(f"new thread: {self._thread_key}", "dim")
            return
        if name == "quit":
            await self._conn.close()
            self.exit()
            return
        await self._conn.send(command_frame(self._thread_key, name, arg))
