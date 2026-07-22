"""System-prompt lines describing the host shell: OS, dialect, and contract.

Split from :mod:`chief.tools.shell.service` (which owns the tool + service) so the
prompt concerns live apart from dispatch; ``HOST_SHELL_CONTRACT`` is single-sourced
here and quoted by the tool description so the two can't drift.
"""

import platform
from pathlib import Path

from chief.tools.shell.frame import resolve_shell

#: The host shell's invariants, single-sourced so the tool description and the
#: system prompt can't drift: the owner's real machine; chain steps in one command.
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
