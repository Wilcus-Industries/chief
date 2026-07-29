"""What the installer must run to give chief its own system account.

A pure plan: each step is a description plus the exact argv, so the tests pin
every generated command byte-for-byte and the installer can print the whole
plan before touching the machine. Passwords never appear in argv — they travel
in ``stdin``, the one field the pinned commands do not carry.

``run_as`` says whose authority a step needs: ``None`` is the owner running the
installer, ``"root"`` escalates, and chief's own name is used for the steps
that must land in chief's home (its git identity).
"""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER = "chief"
DEFAULT_GROUP = "chief"
DEFAULT_TREE = Path("/opt/chief")
DEFAULT_EMAIL = "chief@localhost"
SECRETS_DIRNAME = "secrets"

__all__ = [
    "DEFAULT_EMAIL",
    "DEFAULT_GROUP",
    "DEFAULT_TREE",
    "DEFAULT_USER",
    "AccountPlan",
    "Step",
    "account_plan",
    "default_home",
]


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


@dataclass(frozen=True)
class AccountPlan:
    """The account chief will run as, and every step to get there."""

    user: str
    group: str
    home: Path
    tree: Path
    steps: tuple[Step, ...]


def default_home(platform: str, user: str) -> Path:
    return Path("/Users" if platform == "darwin" else "/home") / user


def _create_steps(
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


def _group_steps(
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


def _permission_steps(tree: Path, user: str, group: str) -> tuple[Step, ...]:
    return (
        Step(
            "give the tree to chief, shared with the group",
            ("chown", "-R", f"{user}:{group}", str(tree)),
            run_as="root",
        ),
        Step(
            "let the group read and edit the tree",
            ("chmod", "-R", "g+rwX", str(tree)),
            run_as="root",
        ),
        Step(
            "make new files inherit the shared group",
            ("find", str(tree), "-type", "d", "-exec", "chmod", "g+s", "{}", "+"),
            run_as="root",
        ),
        Step(
            # After the sweep above, or it would re-open what it just closed.
            "carve out secrets — readable by chief only",
            ("chmod", "-R", "go-rwx", str(tree / SECRETS_DIRNAME)),
            run_as="root",
        ),
    )


def _git_steps(tree: Path, user: str, email: str) -> tuple[Step, ...]:
    return (
        Step(
            "give chief a git identity (self-edit commits fail without one)",
            ("git", "config", "--global", "user.name", user),
            run_as=user,
        ),
        Step(
            "give chief a git email",
            ("git", "config", "--global", "user.email", email),
            run_as=user,
        ),
        Step(
            # git refuses to operate on a tree owned by another user.
            "trust the tree in your own git config",
            ("git", "config", "--global", "--add", "safe.directory", str(tree)),
        ),
    )


def account_plan(
    *,
    platform: str,
    owner: str,
    create: bool = True,
    password: str | None = None,
    user: str = DEFAULT_USER,
    group: str = DEFAULT_GROUP,
    tree: Path = DEFAULT_TREE,
    home: Path | None = None,
    email: str = DEFAULT_EMAIL,
) -> AccountPlan:
    """Every step to stand chief up as its own user, in order.

    ``create=False`` installs into an account that already exists: creation is
    skipped, the rest still runs. Raises ``ValueError`` on an unsupported
    platform or a creating plan with no password.
    """
    if platform not in ("darwin", "linux"):
        raise ValueError(f"unsupported platform: {platform!r}")
    if create and not password:
        raise ValueError("creating an account needs a password")
    resolved_home = home or default_home(platform, user)
    steps = (
        *(
            _create_steps(platform, user, resolved_home, password or "")
            if create
            else ()
        ),
        *_group_steps(platform, user, group, owner),
        *_permission_steps(tree, user, group),
        *_git_steps(tree, user, email),
        *(
            (
                Step(
                    "keep chief running without an interactive login",
                    ("loginctl", "enable-linger", user),
                    run_as="root",
                ),
            )
            if platform == "linux"
            else ()
        ),
    )
    return AccountPlan(
        user=user, group=group, home=resolved_home, tree=tree, steps=steps
    )
