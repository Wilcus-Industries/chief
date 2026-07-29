"""Standing chief up as its own system user: ask, show the plan, run it.

A scripted run refuses outright — creating a system account is not something
an unattended installer should do behind the owner's back. Declining anywhere
falls back to exactly today's single-user install, which stays supported.

Escalation prompts land on the terminal rather than on this process's stdin,
which is what lets the whole thing work under ``curl … | bash``: ``sudo``
reads the tty directly, so the piped installer never has to forward it.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from chief.install.account import DEFAULT_TREE, Step, account_plan, grant_steps
from chief.install.dedicated_ask import (
    CREATE,
    DECLINED,
    EXISTING,
    Answers,
    ask,
)
from chief.install.session import (
    NO_SESSION,
    SessionPlan,
    disk_encrypted,
    session_plan,
)
from chief.install.wizard_io import WizardIO

REFUSED = "refused"

#: Runs one step for real, letting sudo talk to the terminal.
StepRunner = Callable[[Step], "subprocess.CompletedProcess[str]"]

__all__ = ["REFUSED", "Setup", "set_dedicated_mode", "setup_account"]


def default_step_runner(step: Step) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(step.command()), input=step.stdin, text=True, check=False
    )


@dataclass(frozen=True)
class Setup:
    """What the install ended up with, and what is left for the human."""

    mode: str
    user: str
    session: str
    manual: tuple[str, ...]

    home: Path | None = None

    @property
    def dedicated(self) -> bool:
        return self.mode in (CREATE, EXISTING)

    def report(self) -> str:
        """The facts install.sh needs, as ``key=value`` lines."""
        return (
            f"mode={self.mode}\nuser={self.user}\n"
            f"home={self.home or ''}\nsession={self.session}\n"
        )


def set_dedicated_mode(config_path: Path) -> bool:
    """Flip ``imessage.mode`` to ``dedicated`` in place.

    A surgical edit, not a YAML re-dump: config.yaml is seeded from the
    commented template and a round-trip would strip every comment.
    """
    if not config_path.exists():
        return False
    text = config_path.read_text()
    match = re.search(r"^(\s*)mode:[ \t]*\S+[ \t]*$", text, re.M)
    if match is None:
        return False
    config_path.write_text(
        text[: match.start()] + f"{match.group(1)}mode: dedicated"
        + text[match.end():]
    )
    return True


def _run(steps: tuple[Step, ...], io: WizardIO, execute: StepRunner) -> None:
    for step in steps:
        io.say(f"  {step.description}")
        result = execute(step)
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(step.command())} failed ({result.returncode})"
            )


def setup_account(
    *,
    platform: str,
    owner: str,
    home: Path,
    io: WizardIO,
    interactive: bool,
    tree: Path = DEFAULT_TREE,
    config_path: Path = Path("config.yaml"),
    encrypted: bool | None = None,
    execute: StepRunner = default_step_runner,
) -> Setup:
    """Offer the dedicated account and, if taken, build it.

    ``encrypted`` overrides the disk-encryption probe (tests, and a caller
    that already asked). Raises ``RuntimeError`` on the first failed step —
    a half-built account is worse when it is also silent.
    """
    if not interactive:
        io.say(
            "account: a non-interactive run will not create a system "
            "account — chief runs as you. Re-run `chief wizard` on a "
            "terminal to change that."
        )
        return Setup(REFUSED, owner, NO_SESSION, ())
    answers = ask(io, home=home)
    if answers.choice == DECLINED:
        return Setup(DECLINED, owner, NO_SESSION, ())
    if encrypted is None:
        encrypted = disk_encrypted(platform)
    session = session_plan(
        platform=platform,
        encrypted=encrypted,
        user=answers.user,
        password=answers.password or None,
    )
    plan = account_plan(
        platform=platform,
        owner=owner,
        create=answers.choice == CREATE,
        password=answers.password or None,
        user=answers.user,
        tree=tree,
    )
    steps = (
        *plan.steps,
        *grant_steps(
            group=plan.group, read=answers.read, write=answers.write
        ),
        *session.steps,
    )
    io.say(f"About to run {len(steps)} steps as root — sudo will prompt:")
    for step in steps:
        io.say(f"    {' '.join(step.command())}")
        if step.stdin:
            io.say("      (a password is passed on stdin, not shown)")
    if io.prompt("  run them? [y/N]: ").strip().lower() not in ("y", "yes"):
        io.say("account: declined — chief runs as you, exactly as before.")
        return Setup(DECLINED, owner, NO_SESSION, ())
    _run(steps, io, execute)
    if not set_dedicated_mode(config_path):
        io.say("  note: could not set imessage.mode — set it to `dedicated`.")
    return Setup(
        answers.choice,
        answers.user,
        session.mechanism,
        _manual(answers, session),
        home=plan.home,
    )


def _manual(answers: Answers, session: SessionPlan) -> tuple[str, ...]:
    return (
        "group membership does not reach already-open shells — open a new "
        "one, or log out and back in, before editing chief's tree",
        f"sign {answers.user} into its own Apple ID in Messages, from its "
        f"own login session — imessage.mode is already `dedicated`",
        f"make sure `uv` is on {answers.user}'s PATH — the service runs the "
        f"launcher, which runs uv",
        *session.manual,
        "the daemon is not started: its service loads at "
        f"{answers.user}'s next login",
    )
