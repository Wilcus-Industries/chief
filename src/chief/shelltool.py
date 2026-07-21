"""The owner's ``shell`` tool: the registry-facing surface over :mod:`chief.shellhost`.

``register_shell_tool`` wires a single ``shell`` tool with ``wants_context=True`` so one
registered tool routes each call to its own thread's persistent shell by ``thread_key``.
``shell`` mutates the host and so is not read-only — the gate (:mod:`chief.gate`) raises
an approval card on every command until the owner "always allow"s it, exactly like
``write_file``/``edit_file``/``restart``. ``ShellService`` owns the shells and is closed
by the daemon at shutdown; the prompt-side labels live in :mod:`chief.shellprompt`.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.provider.base import ToolSpec
from chief.shellhost import ShellHost
from chief.shellprompt import HOST_SHELL_CONTRACT


def format_shell_result(result: dict[str, Any]) -> str:
    """Render a shell result dict as the model-visible string the tool returns.

    Mirrors the file tools' plain-string style: stdout first, then ``[stderr]`` /
    ``[exit code N]`` / ``[output truncated]`` sections only when they carry signal.
    """
    stdout = str(result.get("stdout", ""))
    stderr = str(result.get("stderr", ""))
    exit_code = int(result.get("exit_code", 0))
    truncated = bool(result.get("truncated", False))
    segments: list[str] = []
    if stdout:
        segments.append(stdout)
    if stderr:
        segments.append(f"[stderr]\n{stderr}")
    if exit_code != 0:
        segments.append(f"[exit code {exit_code}]")
    if truncated:
        segments.append("[output truncated]")
    return "\n".join(segments) if segments else "(no output)"


@dataclass
class ShellService:
    """Workspace + limits for the host shell; owns the live per-thread shells.

    Creates one :class:`~chief.shellhost.ShellHost` lazily (so the shell binary is
    resolved only when a command actually runs). The daemon calls :meth:`aclose` at
    shutdown to tear the shells down cleanly.
    """

    workspace_dir: str
    timeout_seconds: float
    output_limit: int
    _host: ShellHost | None = field(default=None, init=False, repr=False)

    def _ensure_host(self) -> ShellHost:
        if self._host is None:
            self._host = ShellHost(
                workdir=self.workspace_dir,
                timeout=self.timeout_seconds,
                output_limit=self.output_limit,
            )
        return self._host

    async def run(
        self, thread_key: str, command: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Run one command on ``thread_key``'s persistent shell; return its dict.

        ``timeout`` overrides :attr:`timeout_seconds` for this one call (the agent
        passes it for genuinely slow work); ``None`` uses the configured default.
        """
        result = await self._ensure_host().execute(
            thread_key, command, timeout=timeout
        )
        return result.as_dict()

    async def aclose(self) -> None:
        """Terminate every live shell (clean teardown)."""
        if self._host is not None:
            await self._host.aclose()


_SHELL_SPEC = ToolSpec(
    name="shell",
    description=(
        "Run a shell command on the host and return its stdout/stderr and exit code. "
        "Shell state — environment variables, the current directory, background jobs — "
        "persists across calls within a thread (but not across a restart). The working "
        f"directory starts at the repo root. {HOST_SHELL_CONTRACT}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run on the host.",
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Optional. Seconds before the command is killed. Defaults to 20; "
                    "raise it for genuinely slow work (installs, clones, builds). A "
                    "killed command returns exit code 124 and loses its shell state."
                ),
            },
        },
        "required": ["command"],
    },
)


#: A pre-flight over the raw command string: return a refusal message to block
#: the command, or ``None`` to let it run. Guards are mechanical seatbelts for
#: hazards prompts alone can't be trusted to prevent (e.g. the iMessage
#: owner-handle echo loop) — keep them few and specific.
ShellGuard = Callable[[str], str | None]

#: Exit code reported for a command a guard refused — nothing ran.
GUARD_REFUSED_EXIT_CODE = 126


def refuse(guards: Sequence[ShellGuard], command: str) -> str | None:
    """First guard refusal for ``command``, or ``None`` when all of them pass."""
    for guard in guards:
        refusal = guard(command)
        if refusal is not None:
            return refusal
    return None


def guarded_runner(
    service: "ShellService", guards: Sequence[ShellGuard] = ()
) -> Callable[[str, str], Awaitable[dict[str, Any]]]:
    """Wrap :meth:`ShellService.run` so an *unattended* caller gets the guards too.

    The guards are wired into the ``shell`` tool, so a caller that reaches
    ``ShellService.run`` directly (cron's command schedules) would otherwise
    bypass seatbelts like the iMessage owner-handle echo loop — and do it with
    nobody watching. A refusal returns a normal result dict, never a raise.
    """

    async def run(thread_key: str, command: str) -> dict[str, Any]:
        refusal = refuse(guards, command)
        if refusal is not None:
            return {
                "stdout": "",
                "stderr": refusal,
                "exit_code": GUARD_REFUSED_EXIT_CODE,
                "truncated": False,
            }
        return await service.run(thread_key, command)

    return run


def register_shell_tool(
    registry: ToolRegistry,
    service: ShellService,
    guards: Sequence[ShellGuard] = (),
) -> None:
    """Expose the single ``shell`` tool, routing each call to its thread's shell.

    ``wants_context=True`` delivers the calling :class:`ToolContext` so one registered
    tool addresses every thread's own persistent shell by ``thread_key``. A spawn
    failure surfaces as an error string, never a raise.
    """

    async def shell(
        command: str,
        timeout: float | None = None,
        context: ToolContext | None = None,
    ) -> str:
        refusal = refuse(guards, command)
        if refusal is not None:
            return refusal
        thread_key = context.thread_key if context is not None else "default"
        try:
            result = await service.run(thread_key, command, timeout=timeout)
        except Exception as exc:  # shell spawn failed — surface, don't crash the loop
            return f"error: shell unavailable: {exc}"
        return format_shell_result(result)

    registry.register(Tool(_SHELL_SPEC, shell, wants_context=True))
