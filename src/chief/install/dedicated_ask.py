"""The dedicated-account questions: what to create, and what chief may reach.

Asking is separated from doing (:mod:`.dedicated`) so the answers can be
inspected and pinned without a terminal or a machine to change.
"""

from dataclasses import dataclass, field
from pathlib import Path

from chief.install.account import DEFAULT_USER, grant_reason
from chief.install.session import password_conflict
from chief.install.wizard_io import MIN_PASSWORD_LENGTH, WizardIO

CREATE = "create"
EXISTING = "existing"
DECLINED = "declined"

__all__ = ["CREATE", "DECLINED", "EXISTING", "Answers", "ask"]


@dataclass(frozen=True)
class Answers:
    """What the owner chose. ``password`` is empty unless creating."""

    choice: str
    user: str = DEFAULT_USER
    password: str = ""
    read: tuple[Path, ...] = field(default_factory=tuple)
    write: tuple[Path, ...] = field(default_factory=tuple)


def _password(io: WizardIO, user: str) -> str:
    while True:
        password = io.prompt_secret(
            f"  {user}'s login password (min {MIN_PASSWORD_LENGTH} chars): "
        )
        if len(password) < MIN_PASSWORD_LENGTH:
            io.say(f"  must be at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if password != io.prompt_secret("  confirm: "):
            io.say("  passwords do not match — try again.")
            continue
        # Only asked so it can be rejected: macOS refuses automatic login when
        # the two match, and it fails silently rather than telling you.
        apple = io.prompt_secret(
            "  chief's Apple ID password, to check it differs "
            "(never stored, empty to skip): "
        )
        conflict = password_conflict(password, apple)
        if conflict:
            io.say(f"  {conflict} — pick another login password.")
            continue
        return password


def _dirs(io: WizardIO, verb: str, home: Path) -> tuple[Path, ...]:
    raw = io.prompt(
        f"  directories chief may {verb}, space-separated (empty = none): "
    )
    chosen: list[Path] = []
    for token in raw.split():
        path = Path(token).expanduser()
        reason = grant_reason(path, home)
        if reason:
            io.say(f"  skipped — {reason}")
            continue
        chosen.append(path)
    return tuple(chosen)


def ask(io: WizardIO, *, home: Path) -> Answers:
    """Walk the three account cases, then the two file-grant questions."""
    io.say(
        "chief can run as its own system user, with its own Apple ID — you "
        "text it as an ordinary contact instead of texting yourself."
    )
    answer = io.prompt(
        "  [c]reate an account, use an [e]xisting one, or run as you [n]? "
        "[C/e/n]: "
    ).strip().lower()
    if answer in ("n", "no"):
        io.say("account: declined — chief runs as you, exactly as before.")
        return Answers(DECLINED)
    user = (
        io.prompt(f"  account name [{DEFAULT_USER}]: ").strip() or DEFAULT_USER
    )
    choice = EXISTING if answer in ("e", "existing") else CREATE
    password = _password(io, user) if choice == CREATE else ""
    io.say("Which of your directories may chief reach? Default is none.")
    return Answers(
        choice=choice,
        user=user,
        password=password,
        read=_dirs(io, "read", home),
        write=_dirs(io, "read and write", home),
    )
