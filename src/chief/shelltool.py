"""The owner's ``shell`` tool: the registry-facing surface over :mod:`chief.shellhost`.

``register_shell_tool`` wires a single ``shell`` tool with ``wants_context=True`` so one
registered tool routes each call to its own thread's persistent shell by ``thread_key``.
``shell`` mutates the host and so is not read-only — the gate (:mod:`chief.gate`) raises
an approval card on every command until the owner "always allow"s it, exactly like
``write_file``/``edit_file``/``restart``. ``ShellService`` owns the shells and is closed
by the daemon at shutdown; ``shell_prompt_line`` tells the agent which shell dialect it
is actually speaking.
"""

import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chief.agent.tools import Tool, ToolContext, ToolRegistry
from chief.provider.base import ToolSpec
from chief.shellframe import resolve_shell
from chief.shellhost import ShellHost

#: The host shell's invariants, single-sourced so the tool description and the system
#: prompt can't drift: the owner's real machine; chain steps in one command.
HOST_SHELL_CONTRACT = (
    "The shell runs directly on the owner's machine as their user, with their full "
    "environment. Chain steps with && in one command rather than relying on separate "
    "calls."
)


def shell_label() -> str:
    """The bare name of the shell the tool will drive (``zsh``/``bash``/…).

    Names the dialect for the system prompt so the agent can't assume bash on a zsh
    host. Best-effort: ``"a shell"`` if none resolves, so prompt assembly never fails.
    """
    try:
        return Path(resolve_shell()[0]).name
    except RuntimeError:
        return "a shell"


def host_label() -> str:
    """The OS the daemon runs on, for the system prompt.

    ``Darwin`` → ``macOS <ver>`` (BSD userland — ``sed -i ''``, ``pbcopy``, no ``apt``).
    Linux → the distro's ``PRETTY_NAME`` from ``/etc/os-release`` (``Arch Linux``,
    ``Ubuntu 22.04.4 LTS``) so the agent picks the right package manager; a bare
    ``Linux`` if that file is absent. Any other platform reports its own name.
    """
    system = platform.system()
    if system == "Darwin":
        version = platform.mac_ver()[0]
        return f"macOS {version}".strip()
    if system == "Linux":
        try:
            pretty = platform.freedesktop_os_release().get("PRETTY_NAME", "").strip()
        except OSError:
            pretty = ""
        return pretty or "Linux"
    return system or "an unknown OS"


def shell_prompt_line() -> str:
    """One line for the system prompt: the host OS, the shell tool, and its dialect."""
    return (
        f"\n\nYou are running as a daemon on {host_label()}. You have a `shell` tool "
        f"that runs commands on the host via {shell_label()} — mind the OS and that "
        f"dialect (not necessarily Linux or bash). {HOST_SHELL_CONTRACT} Discover and "
        "install capability packages with it (`chief-pkg list`/`search`); see the "
        "package-manager skill."
    )


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

    async def run(self, thread_key: str, command: str) -> dict[str, Any]:
        """Run one command on ``thread_key``'s persistent shell; return its dict."""
        result = await self._ensure_host().execute(thread_key, command)
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
            }
        },
        "required": ["command"],
    },
)


def register_shell_tool(registry: ToolRegistry, service: ShellService) -> None:
    """Expose the single ``shell`` tool, routing each call to its thread's shell.

    ``wants_context=True`` delivers the calling :class:`ToolContext` so one registered
    tool addresses every thread's own persistent shell by ``thread_key``. A spawn
    failure surfaces as an error string, never a raise.
    """

    async def shell(command: str, context: ToolContext | None = None) -> str:
        thread_key = context.thread_key if context is not None else "default"
        try:
            result = await service.run(thread_key, command)
        except Exception as exc:  # shell spawn failed — surface, don't crash the loop
            return f"error: shell unavailable: {exc}"
        return format_shell_result(result)

    registry.register(Tool(_SHELL_SPEC, shell, wants_context=True))
