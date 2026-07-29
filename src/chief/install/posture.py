"""The boot check: what chief's login posture actually is right now.

macOS automatic login is documented to break silently after an OS update, and
a graphical session is the thing Messages delivers into — so the state has to
be visible rather than inferred from chief going quiet. Read on demand (the
web UI polls it, ``chief status`` prints it) rather than kept by a background
loop: none of these probes is expensive and there is no state worth holding.
"""

from dataclasses import dataclass

from chief.install.session import disk_encrypted
from chief.install.units import Runner, default_runner

LOGIN_WINDOW = "/Library/Preferences/com.apple.loginwindow"
NOT_APPLICABLE = "n/a"
UNKNOWN = "unknown"
OFF = "off"
ON = "on"
PRESENT = "present"
ABSENT = "absent"

__all__ = ["Posture", "read_posture"]


@dataclass(frozen=True)
class Posture:
    """Disk encryption, automatic login and session presence, as read."""

    user: str
    encryption: str
    auto_login: str
    session: str

    def problems(self) -> tuple[str, ...]:
        """Everything about this posture that will cost chief its session."""
        found = []
        if self.session == ABSENT:
            found.append(
                f"no graphical session for {self.user} — "
                "Messages will not deliver"
            )
        if self.encryption == OFF and self.auto_login != self.user:
            found.append(
                f"automatic login is not set to {self.user} — a reboot leaves "
                "chief with no session"
            )
        if self.encryption == ON and self.auto_login == self.user:
            found.append(
                "automatic login is set but the disk is encrypted — "
                "macOS ignores it; use the screen-sharing reconnect step"
            )
        return tuple(found)

    def summary(self) -> str:
        return "; ".join(self.problems()) or "ok"


def read_posture(
    *, platform: str, user: str, uid: int, runner: Runner = default_runner
) -> Posture:
    """Probe the machine. Linux has no graphical session to lose — all n/a."""
    if platform != "darwin":
        return Posture(user, NOT_APPLICABLE, NOT_APPLICABLE, NOT_APPLICABLE)
    encrypted = disk_encrypted(platform, runner)
    auto = runner(["defaults", "read", LOGIN_WINDOW, "autoLoginUser"])
    # A GUI domain only exists once someone has actually logged in as that uid.
    session = runner(["launchctl", "print", f"gui/{uid}"])
    return Posture(
        user=user,
        encryption=UNKNOWN if encrypted is None else (ON if encrypted else OFF),
        auto_login=(auto.stdout.strip() if auto.returncode == 0 else "") or OFF,
        session=PRESENT if session.returncode == 0 else ABSENT,
    )
