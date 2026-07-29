"""The ``Step`` primitive, and the account/group commands that differ by OS.

Split from :mod:`.account` so that module stays under the file-length cap: the
plan (what runs, in what order) lives there, the platform-divergent argv lives
here. Both halves stay pure — nothing in this file touches the machine.

``run_as`` says whose authority a step needs: ``None`` is the owner running the
installer, ``"root"`` escalates, and chief's own name is used for the steps that
must land in chief's home (its git identity).
"""

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Step", "create_steps", "group_steps", "remove_steps"]


@dataclass(frozen=True)
class Step:
    """One command the installer runs, and whose authority it needs."""

    description: str
    argv: tuple[str, ...]
    run_as: str | None = None
    stdin: str | None = None

    @property
    def privileged(self) -> bool:
        return self.run_as == "root"

    def command(self) -> tuple[str, ...]:
        """The argv as actually invoked, escalation prefix included."""
        if self.run_as is None:
            return self.argv
        if self.run_as == "root":
            return ("sudo", *self.argv)
        return ("sudo", "-u", self.run_as, *self.argv)


def create_steps(
    platform: str, user: str, home: Path, password: str
) -> tuple[Step, ...]:
    if platform == "darwin":
        return (
            Step(
                # Deliberately not -admin: chief logs in graphically, and an
                # admin chief would be root-equivalent via its shell tool.
                f"create the {user} account (non-admin)",
                (
                    "sysadminctl",
                    "-addUser",
                    user,
                    "-fullName",
                    user,
                    "-home",
                    str(home),
                    "-shell",
                    "/bin/zsh",
                    "-password",
                    "-",
                ),
                run_as="root",
                stdin=f"{password}\n",
            ),
        )
    return (
        Step(
            f"create the {user} account",
            (
                "useradd",
                "--create-home",
                "--home-dir",
                str(home),
                "--shell",
                "/bin/bash",
                user,
            ),
            run_as="root",
        ),
        Step(
            f"set {user}'s login password",
            ("chpasswd",),
            run_as="root",
            stdin=f"{user}:{password}\n",
        ),
    )


def group_steps(
    platform: str, user: str, group: str, owner: str
) -> tuple[Step, ...]:
    if platform == "darwin":
        add = (
            Step(
                f"add {member} to the shared group",
                ("dseditgroup", "-o", "edit", "-a", member, "-t", "user", group),
                run_as="root",
            )
            for member in (user, owner)
        )
        return (
            Step(
                "create the shared group",
                ("dseditgroup", "-o", "create", group),
                run_as="root",
            ),
            *add,
        )
    return (
        Step(
            "create the shared group",
            ("groupadd", "--force", group),
            run_as="root",
        ),
        *(
            Step(
                f"add {member} to the shared group",
                ("usermod", "-aG", group, member),
                run_as="root",
            )
            for member in (user, owner)
        ),
    )


def remove_steps(platform: str, user: str, group: str) -> tuple[Step, ...]:
    """Delete chief's account, its home and the shared group.

    Destructive and never the default: chief's home holds its message store,
    which the dedicated Apple ID's whole conversation lives in.
    """
    if platform == "darwin":
        return (
            Step(
                f"delete the {user} account and its home",
                ("sysadminctl", "-deleteUser", user, "-secure"),
                run_as="root",
            ),
            Step(
                "delete the shared group",
                ("dseditgroup", "-o", "delete", group),
                run_as="root",
            ),
        )
    return (
        Step(
            f"delete the {user} account and its home",
            ("userdel", "--remove", user),
            run_as="root",
        ),
        Step(
            "delete the shared group",
            ("groupdel", "--force", group),
            run_as="root",
        ),
    )
