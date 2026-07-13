"""Unit tests for the installer wizard (#154).

The wizard replaces the old manual secrets checklist: it captures the owner
password (the web UI credential) and one model-auth path (Copilot login walk or
OpenRouter key paste). Every side effect is injected — prompts, the OpenRouter
validator, the Copilot login probe — so these tests run without a TTY or network.

The load-bearing assertions:
- the credential the wizard writes is verified by the *web surface's own*
  ``WebAuth`` (format identity, not a lookalike);
- re-runs are idempotent (existing state is kept, never destroyed);
- the OpenRouter key is validated before it is written, and written 0600.
"""

import stat
from pathlib import Path

from chief.install.wizard import WizardIO, run_wizard
from chief.web.auth import PASSWORD_FILE, WebAuth


class ScriptedIO(WizardIO):
    """A WizardIO fed from canned answers, recording everything said."""

    def __init__(
        self, answers: list[str] | None = None, secrets: list[str] | None = None
    ) -> None:
        self.answers = list(answers or [])
        self.secrets = list(secrets or [])
        self.lines: list[str] = []
        super().__init__(
            prompt=lambda _msg: self.answers.pop(0),
            prompt_secret=lambda _msg: self.secrets.pop(0),
            say=self.lines.append,
        )

    def said(self, fragment: str) -> bool:
        return any(fragment in line for line in self.lines)


def _no_copilot() -> bool:
    return False


def _copilot_ok() -> bool:
    return True


async def _reject_key(_key: str) -> str | None:
    return "OpenRouter rejected the credential (HTTP 401)"


async def _accept_key(_key: str) -> str | None:
    return None


def test_password_step_writes_web_verifiable_credential(tmp_path: Path) -> None:
    """The wizard's credential must verify through the web UI's own WebAuth."""
    io = ScriptedIO(answers=["2"], secrets=["hunter2boogaloo", "hunter2boogaloo"])

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_copilot_ok,
        validate_openrouter=_accept_key,
        env={},
    )

    assert result.password == "set"
    auth = WebAuth(tmp_path)
    assert auth.password_set
    assert auth.verify("hunter2boogaloo")
    assert not auth.verify("wrong")
    stored = (tmp_path / PASSWORD_FILE).read_text()
    assert stored.startswith("scrypt$"), "must be the web auth at-rest format"


def test_password_too_short_then_mismatch_then_success(tmp_path: Path) -> None:
    """Short and mismatched attempts re-prompt instead of writing bad state."""
    io = ScriptedIO(
        answers=["2"],
        secrets=[
            "short",  # < MIN_PASSWORD_LENGTH → rejected
            "longenough1",
            "different1",  # mismatch → rejected
            "longenough1",
            "longenough1",  # accepted
        ],
    )

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_copilot_ok,
        validate_openrouter=_accept_key,
        env={},
    )

    assert result.password == "set"
    assert WebAuth(tmp_path).verify("longenough1")
    assert io.said("at least 8")
    assert io.said("do not match")


def test_existing_password_is_kept_never_destroyed(tmp_path: Path) -> None:
    """Idempotent re-run: an existing credential (and its sessions) survive."""
    auth = WebAuth(tmp_path)
    auth.set_password("originalpass")
    token = auth.issue_session()
    io = ScriptedIO(answers=["2"], secrets=[])

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_copilot_ok,
        validate_openrouter=_accept_key,
        env={},
    )

    assert result.password == "kept"
    assert WebAuth(tmp_path).verify("originalpass")
    assert WebAuth(tmp_path).session_valid(token), "sessions must not be revoked"


def test_env_password_supports_headless_installs(tmp_path: Path) -> None:
    """CHIEF_OWNER_PASSWORD drives the non-interactive (curl|bash CI) path."""
    io = ScriptedIO()

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=False,
        copilot_check=_copilot_ok,
        validate_openrouter=_accept_key,
        env={"CHIEF_OWNER_PASSWORD": "fromenvpass1"},
    )

    assert result.password == "set"
    assert WebAuth(tmp_path).verify("fromenvpass1")


def test_non_interactive_without_env_defers_to_web_setup(tmp_path: Path) -> None:
    """No TTY and no env password: skip — the browser /setup captures it."""
    io = ScriptedIO()

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=False,
        copilot_check=_copilot_ok,
        validate_openrouter=_accept_key,
        env={},
    )

    assert result.password == "skipped"
    assert not WebAuth(tmp_path).password_set
    assert io.said("/setup")


def test_short_env_password_is_refused_not_written(tmp_path: Path) -> None:
    """A too-short env password is refused loudly; nothing is written."""
    io = ScriptedIO()

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=False,
        copilot_check=_copilot_ok,
        validate_openrouter=_accept_key,
        env={"CHIEF_OWNER_PASSWORD": "short"},
    )

    assert result.password == "skipped"
    assert not WebAuth(tmp_path).password_set
    assert io.said("at least 8")


def test_openrouter_key_validated_then_written_0600(tmp_path: Path) -> None:
    """The OpenRouter path validates before writing, and writes owner-only."""
    seen: list[str] = []

    async def validate(key: str) -> str | None:
        seen.append(key)
        return None

    io = ScriptedIO(
        answers=["2"],
        secrets=["passwordpass1", "passwordpass1", "sk-or-v1-goodkey"],
    )

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_no_copilot,
        validate_openrouter=validate,
        env={},
    )

    assert result.model_auth == "openrouter"
    assert seen == ["sk-or-v1-goodkey"]
    key_file = tmp_path / "openrouter_api_key"
    assert key_file.read_text() == "sk-or-v1-goodkey"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_bad_openrouter_key_reprompts_before_writing(tmp_path: Path) -> None:
    """A rejected key is never written; the wizard re-prompts."""
    calls: list[str] = []

    async def validate(key: str) -> str | None:
        calls.append(key)
        return None if key == "sk-good" else "OpenRouter rejected the credential"

    io = ScriptedIO(
        answers=["2"],
        secrets=["passwordpass1", "passwordpass1", "sk-bad", "sk-good"],
    )

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_no_copilot,
        validate_openrouter=validate,
        env={},
    )

    assert result.model_auth == "openrouter"
    assert calls == ["sk-bad", "sk-good"]
    assert (tmp_path / "openrouter_api_key").read_text() == "sk-good"
    assert io.said("rejected")


def test_copilot_walk_rechecks_until_login_lands(tmp_path: Path) -> None:
    """The Copilot path re-checks the CLI login mid-flow instead of aborting."""
    checks = iter([False, False, True])
    io = ScriptedIO(
        answers=["1", "", ""],  # choose copilot, then two Enter-to-recheck rounds
        secrets=["passwordpass1", "passwordpass1"],
    )

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=lambda: next(checks),
        validate_openrouter=_accept_key,
        env={},
    )

    assert result.model_auth == "copilot"
    assert io.said("copilot")


def test_copilot_walk_can_be_skipped(tmp_path: Path) -> None:
    """Typing 's' escapes the re-check loop without failing the install."""
    io = ScriptedIO(
        answers=["1", "s"],
        secrets=["passwordpass1", "passwordpass1"],
    )

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_no_copilot,
        validate_openrouter=_accept_key,
        env={},
    )

    assert result.model_auth == "skipped"


def test_existing_model_auth_is_kept(tmp_path: Path) -> None:
    """Idempotent re-run: an existing OpenRouter key means no model-auth prompts."""
    (tmp_path / "openrouter_api_key").write_text("sk-existing")
    io = ScriptedIO(secrets=["passwordpass1", "passwordpass1"])

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=True,
        copilot_check=_no_copilot,
        validate_openrouter=_reject_key,
        env={},
    )

    assert result.model_auth == "kept"
    assert (tmp_path / "openrouter_api_key").read_text() == "sk-existing"


def test_env_openrouter_key_headless_path(tmp_path: Path) -> None:
    """CHIEF_OPENROUTER_KEY drives model auth without prompts (validated)."""
    io = ScriptedIO()

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=False,
        copilot_check=_no_copilot,
        validate_openrouter=_accept_key,
        env={"CHIEF_OPENROUTER_KEY": "sk-headless"},
    )

    assert result.model_auth == "openrouter"
    assert (tmp_path / "openrouter_api_key").read_text() == "sk-headless"


def test_non_interactive_no_model_auth_warns_and_continues(tmp_path: Path) -> None:
    """Headless with no model auth: warn (chat needs it) but do not fail."""
    io = ScriptedIO()

    result = run_wizard(
        secrets_dir=tmp_path,
        io=io,
        interactive=False,
        copilot_check=_no_copilot,
        validate_openrouter=_reject_key,
        env={},
    )

    assert result.model_auth == "skipped"
    assert io.said("model auth")
