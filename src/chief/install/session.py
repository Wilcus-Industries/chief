"""Which login-session mechanism this machine can support, and how to get it.

Messages only delivers into a real graphical session, and research settled that
none can be manufactured headlessly from a root context. So the mechanism
follows the machine's disk-encryption state: an unencrypted Mac takes the
platform's supported automatic-login path, an encrypted one keeps a documented
human step after every reboot, and Linux needs no session at all — lingering
already covers it.

Pure like :mod:`.account`: the plan is data, pinned by tests, printed by the
installer before anything runs.
"""

from dataclasses import dataclass

from chief.install.account import Step
from chief.install.units import Runner, default_runner

AUTO_LOGIN = "auto-login"
SCREEN_SHARING = "screen-sharing"
NO_SESSION = "none"

_RECONNECT = (
    "after every reboot: unlock the disk at the console as yourself, then",
    "connect Screen Sharing to vnc://127.0.0.1 and log in as {user} — that",
    "login is what creates the graphical session Messages delivers into",
)

__all__ = [
    "AUTO_LOGIN",
    "NO_SESSION",
    "SCREEN_SHARING",
    "SessionPlan",
    "disk_encrypted",
    "password_conflict",
    "session_plan",
]


@dataclass(frozen=True)
class SessionPlan:
    """How chief gets a graphical session, and what stays on the human."""

    mechanism: str
    steps: tuple[Step, ...]
    manual: tuple[str, ...]


def disk_encrypted(
    platform: str, runner: Runner = default_runner
) -> bool | None:
    """macOS disk-encryption state from ``fdesetup status``.

    ``None`` means unknown — a failed call, unparseable output, or a platform
    where the question does not apply. Callers must treat it as encrypted:
    automatic login on an encrypted disk silently does nothing.
    """
    if platform != "darwin":
        return None
    result = runner(["fdesetup", "status"])
    if result.returncode != 0:
        return None
    if "FileVault is On" in result.stdout:
        return True
    if "FileVault is Off" in result.stdout:
        return False
    return None


def password_conflict(login_password: str, apple_id_password: str) -> str | None:
    """macOS refuses automatic login when the two passwords match."""
    if login_password and login_password == apple_id_password:
        return (
            "chief's login password must differ from its Apple ID password — "
            "macOS refuses automatic login when they match"
        )
    return None


def session_plan(
    *,
    platform: str,
    encrypted: bool | None,
    user: str,
    password: str | None = None,
) -> SessionPlan:
    """The session mechanism for this machine, with its steps and human notes.

    Raises ``ValueError`` if the automatic-login branch is asked for without
    chief's login password.
    """
    if platform not in ("darwin", "linux"):
        raise ValueError(f"unsupported platform: {platform!r}")
    if platform == "linux":
        return SessionPlan(NO_SESSION, (), ())
    if encrypted is False:
        if not password:
            raise ValueError("automatic login needs chief's login password")
        return SessionPlan(
            AUTO_LOGIN,
            (
                Step(
                    f"log {user} in automatically at boot",
                    (
                        "sysadminctl",
                        "-autologin",
                        "set",
                        "-userName",
                        user,
                        "-password",
                        "-",
                    ),
                    run_as="root",
                    stdin=f"{password}\n",
                ),
            ),
            (
                "automatic login breaks silently after OS updates — the boot",
                "check reports it, and `chief status` shows the same state",
            ),
        )
    unknown = encrypted is None
    return SessionPlan(
        SCREEN_SHARING,
        (
            Step(
                "allow screen sharing so you can open chief's session",
                ("launchctl", "enable", "system/com.apple.screensharing"),
                run_as="root",
            ),
            Step(
                "start the screen-sharing service",
                (
                    "launchctl",
                    "load",
                    "-w",
                    "/System/Library/LaunchDaemons/com.apple.screensharing.plist",
                ),
                run_as="root",
            ),
        ),
        (
            *(
                ("could not read the disk-encryption state — assuming encrypted",)
                if unknown
                else ()
            ),
            *(line.format(user=user) for line in _RECONNECT),
        ),
    )
