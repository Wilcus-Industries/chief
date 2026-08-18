"""What the installer must run to give chief its own system account.

A pure plan: each step is a description plus the exact argv, so the tests pin
every generated command byte-for-byte and the installer can print the whole
plan before touching the machine. Passwords never appear in argv — they travel
in ``stdin``, the one field the pinned commands do not carry.

The ``Step`` primitive and the account/group commands that differ by OS live
in :mod:`.account_steps`; this module is the order they run in.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chief.install.account_steps import Step, create_steps, group_steps

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
    "grant_steps",
]


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


def grant_steps(
    *,
    group: str = DEFAULT_GROUP,
    read: Sequence[Path] = (),
    write: Sequence[Path] = (),
) -> tuple[Step, ...]:
    """Group permissions for the owner directories chief may reach.

    Read grants stop at ``g+rX``; write grants add ``g+w`` and the setgid bit
    so files chief creates stay in the shared group.
    """
    steps: list[Step] = []
    for path, mode, verb in (
        *((p, "g+rX", "read") for p in read),
        *((p, "g+rwX", "write") for p in write),
    ):
        steps.append(
            Step(
                f"let chief {verb} {path}",
                ("chgrp", "-R", group, str(path)),
                run_as="root",
            )
        )
        steps.append(
            Step(
                f"apply {verb} permissions to {path}",
                ("chmod", "-R", mode, str(path)),
                run_as="root",
            )
        )
    for path in write:
        steps.append(
            Step(
                f"keep new files under {path} in the shared group",
                ("find", str(path), "-type", "d", "-exec", "chmod", "g+s", "{}", "+"),
                run_as="root",
            )
        )
    return tuple(steps)


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
            create_steps(platform, user, resolved_home, password or "")
            if create
            else ()
        ),
        *group_steps(platform, user, group, owner),
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
