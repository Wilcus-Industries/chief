"""The release + update system, end-to-end against real git repositories.

Git is never faked here. This feature *is* git behaviour plus conflict
handling, so a mocked runner would assert nothing — the pattern the old
``install/update.py`` tests used and the reason they missed everything.

The central mechanism every test cuts through: the real three-way application
of a box's local layer onto a newer release, against real repos on disk.
"""

import json
import subprocess
from pathlib import Path

import pytest

from chief.install import basepin, layer, migrate, releases, updatecheck
from chief.install.commands import main
from chief.install.release import cut_release
from chief.install.update import abort_update, update


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


def _commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A real upstream repo at v0.3.0, shaped like core's tree."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "release@test")
    git(repo, "config", "user.name", "release")
    _write(repo / "pyproject.toml", '[project]\nname = "chief"\nversion = "0.3.0"\n')
    _write(repo / "src/chief/app.py", "line1\nline2\nline3\n")
    _write(repo / "README.md", "chief\n")
    # Instance data is already outside the lineage — but skills/ is not yet,
    # which is the pre-migration state every existing box is in.
    _write(repo / ".gitignore", "/data/\n/config.yaml\n/secrets/*\n")
    _commit(repo, "v0.3.0")
    git(repo, "tag", "v0.3.0")
    return repo


@pytest.fixture
def box(tmp_path: Path, upstream: Path) -> Path:
    """A real install: cloned from upstream, pinned at v0.3.0, self-edited."""
    repo = tmp_path / "box"
    git(tmp_path, "clone", "-q", str(upstream), str(repo))
    git(repo, "config", "user.email", "box@test")
    git(repo, "config", "user.name", "box")
    basepin.write_base(repo, git(repo, "rev-parse", "v0.3.0^{commit}").strip())
    return repo


def _release(upstream: Path, version: str, edit: Path, text: str) -> str:
    """Cut a new upstream release touching one file."""
    _write(edit, text)
    _write(
        upstream / "pyproject.toml",
        f'[project]\nname = "chief"\nversion = "{version}"\n',
    )
    commit = _commit(upstream, f"release {version}")
    git(upstream, "tag", f"v{version}")
    return commit


def _self_edit(box: Path, path: Path, text: str) -> str:
    _write(path, text)
    return _commit(box, "self-edit: local change")


# --- versions and release resolution ----------------------------------------


def test_parse_version_accepts_only_release_tags() -> None:
    assert releases.parse_version("v1.2.3") == releases.Version(1, 2, 3)
    assert releases.parse_version("1.2.3") is None
    assert releases.parse_version("v1.2") is None
    assert releases.parse_version("v1.2.3-rc1") is None


def test_versions_order_numerically_not_lexically() -> None:
    assert releases.Version(0, 9, 0) < releases.Version(0, 10, 0)
    assert releases.Version(0, 3, 0) < releases.Version(1, 0, 0)


def test_bump_resets_the_lesser_parts() -> None:
    version = releases.Version(0, 3, 4)
    assert str(version.bump("patch")) == "v0.3.5"
    assert str(version.bump("minor")) == "v0.4.0"
    assert str(version.bump("major")) == "v1.0.0"


def test_newest_release_reads_real_tags(upstream: Path) -> None:
    _release(upstream, "0.10.0", upstream / "README.md", "ten\n")
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    newest = releases.newest_release(upstream)
    assert newest is not None
    assert newest.tag == "v0.10.0"  # numeric, not lexical
    assert newest.commit == git(upstream, "rev-parse", "v0.10.0^{commit}").strip()


def test_newest_release_is_none_without_tags(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    git(repo, "init", "-q")
    assert releases.newest_release(repo) is None


def test_project_version_round_trips(upstream: Path) -> None:
    pyproject = upstream / "pyproject.toml"
    assert releases.read_project_version(pyproject) == releases.Version(0, 3, 0)
    releases.write_project_version(pyproject, releases.Version(1, 2, 3))
    assert releases.read_project_version(pyproject) == releases.Version(1, 2, 3)
    assert 'name = "chief"' in pyproject.read_text()


# --- the base pin -----------------------------------------------------------


def test_base_pin_round_trips_as_a_local_ref(box: Path) -> None:
    head = git(box, "rev-parse", "HEAD").strip()
    basepin.write_base(box, head)
    assert basepin.read_base(box) == head
    # A local ref, so no upstream rewrite can move or delete it.
    assert basepin.BASE_REF.startswith("refs/chief/")


def test_base_pin_is_none_when_unset(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q")
    assert basepin.read_base(repo) is None


def test_pending_advances_the_pin_only_once_a_commit_landed(box: Path) -> None:
    """The pin must not move for an update that never reached green."""
    before = git(box, "rev-parse", "HEAD").strip()
    basepin.record_pending(box, commit="deadbeef", version="v0.4.0", head=before)
    assert basepin.resolve_pending(box) is None  # HEAD never moved
    assert basepin.read_pending(box) is None  # ...and the record is cleared
    assert basepin.read_base(box) != "deadbeef"


def test_pending_advances_the_pin_after_a_healthy_boot(box: Path) -> None:
    before = git(box, "rev-parse", "HEAD").strip()
    landed = _self_edit(box, box / "src/chief/app.py", "committed by the seatbelt\n")
    basepin.record_pending(box, commit=landed, version="v0.4.0", head=before)
    assert basepin.resolve_pending(box) == "v0.4.0"
    assert basepin.read_base(box) == landed
    assert basepin.read_pending(box) is None


# --- the three-way layer application ----------------------------------------


def test_clean_update_applies_and_pins_after_health(box: Path, upstream: Path) -> None:
    """The central mechanism: a real local layer onto a real newer release."""
    _self_edit(box, box / "src/chief/local.py", "chief wrote this\n")
    release = _release(upstream, "0.4.0", upstream / "README.md", "chief v0.4.0\n")
    head_before = git(box, "rev-parse", "HEAD").strip()
    said: list[str] = []
    assert update(repo_dir=box, say=said.append) == 0
    # Upstream's change arrived...
    assert (box / "README.md").read_text() == "chief v0.4.0\n"
    # ...the self-edit survived...
    assert (box / "src/chief/local.py").read_text() == "chief wrote this\n"
    # ...and it is staged on the pre-update commit for the seatbelt to commit.
    assert git(box, "rev-parse", "HEAD").strip() == head_before
    assert git(box, "status", "--porcelain").strip()
    assert any("clean" in line for line in said)
    # The pin only advances once the box comes back healthy.
    assert basepin.read_base(box) != release
    _commit(box, "update to v0.4.0")
    assert basepin.resolve_pending(box) == "v0.4.0"
    assert basepin.read_base(box) == release


def test_colliding_update_reports_the_conflicted_files(
    box: Path, upstream: Path
) -> None:
    """A collision must surface, not be resolved by a strategy flag."""
    _self_edit(box, box / "src/chief/app.py", "line1\nLOCAL\nline3\n")
    _release(
        upstream, "0.4.0", upstream / "src/chief/app.py", "line1\nUPSTREAM\nline3\n"
    )
    said: list[str] = []
    assert update(repo_dir=box, say=said.append) == layer.CONFLICTS_EXIT
    report = "\n".join(said)
    assert "src/chief/app.py" in report
    assert "conflict" in report.lower()
    body = (box / "src/chief/app.py").read_text()
    assert "LOCAL" in body and "UPSTREAM" in body  # neither side discarded
    assert "<<<<<<<" in body


def test_local_intent_is_not_silently_discarded(box: Path, upstream: Path) -> None:
    """The old `-X theirs` merge dropped this edit without a word."""
    _self_edit(box, box / "src/chief/app.py", "line1\nLOCAL\nline3\n")
    _release(
        upstream, "0.4.0", upstream / "src/chief/app.py", "line1\nUPSTREAM\nline3\n"
    )
    update(repo_dir=box, say=lambda _: None)
    assert "LOCAL" in (box / "src/chief/app.py").read_text()


def test_update_survives_an_upstream_history_rewrite(
    box: Path, upstream: Path
) -> None:
    """The pin is a local ref, so a rewritten upstream still has a base."""
    _self_edit(box, box / "src/chief/local.py", "chief wrote this\n")
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    assert update(repo_dir=box, say=lambda _: None) == 0
    _commit(box, "update to v0.4.0")
    assert basepin.resolve_pending(box) == "v0.4.0"
    # Upstream rewrites everything: new root, no shared ancestry, tags remade.
    git(upstream, "checkout", "-q", "--orphan", "scrubbed")
    _write(upstream / "README.md", "four\n")
    _write(upstream / "src/chief/app.py", "line1\nline2\nline3\n")
    _write(
        upstream / "pyproject.toml", '[project]\nname = "chief"\nversion = "0.4.0"\n'
    )
    _commit(upstream, "scrubbed history")
    git(upstream, "branch", "-qD", "main")
    git(upstream, "branch", "-qm", "main")
    _release(upstream, "0.5.0", upstream / "README.md", "five\n")
    said: list[str] = []
    assert update(repo_dir=box, say=said.append) == 0
    assert (box / "README.md").read_text() == "five\n"
    assert (box / "src/chief/local.py").read_text() == "chief wrote this\n"


def test_update_is_a_no_op_at_the_newest_release(box: Path) -> None:
    said: list[str] = []
    assert update(repo_dir=box, say=said.append) == 0
    assert not git(box, "status", "--porcelain").strip()
    assert any("up to date" in line for line in said)


def test_update_refuses_a_dirty_tracked_tree(box: Path, upstream: Path) -> None:
    """read-tree would silently discard the uncommitted work."""
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    (box / "src/chief/app.py").write_text("half-finished edit\n")
    said: list[str] = []
    assert update(repo_dir=box, say=said.append) == 1
    assert (box / "src/chief/app.py").read_text() == "half-finished edit\n"
    assert any("uncommitted" in line for line in said)


def test_update_refuses_without_a_base_pin(box: Path, upstream: Path) -> None:
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    git(box, "update-ref", "-d", basepin.BASE_REF)
    said: list[str] = []
    assert update(repo_dir=box, say=said.append) == 1
    assert any("base" in line for line in said)


def test_update_leaves_untracked_instance_data_alone(
    box: Path, upstream: Path
) -> None:
    """Installed skills, config, secrets and data are not in any release tree."""
    _write(box / "skills/self-edit/SKILL.md", "chief's own edit\n")
    _write(box / "config.yaml", "models: {}\n")
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    assert update(repo_dir=box, say=lambda _: None) == 0
    assert (box / "skills/self-edit/SKILL.md").read_text() == "chief's own edit\n"
    assert (box / "config.yaml").read_text() == "models: {}\n"


def test_a_red_check_leaves_the_box_on_the_old_version(
    box: Path, upstream: Path
) -> None:
    """The seatbelt keeps the edits and never commits; the pin must not move.

    Stands in only for the model: the abort is the existing ``revert_edits``
    (``git checkout HEAD -- .``), run here against the real applied tree.
    """
    pinned = basepin.read_base(box)
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    assert update(repo_dir=box, say=lambda _: None) == 0
    git(box, "checkout", "HEAD", "--", ".")  # what revert_edits does
    assert (box / "README.md").read_text() == "chief\n"
    assert basepin.resolve_pending(box) is None
    assert basepin.read_base(box) == pinned


def test_abort_reverts_the_apply_and_forgets_the_pending_record(
    box: Path, upstream: Path
) -> None:
    """Giving up on an update must undo it *and* clear the pending record.

    Reverting the tree alone (``revert_edits``) leaves the record on disk with
    HEAD unmoved; a later unrelated commit then reboots and ``resolve_pending``
    reads that HEAD movement as proof the update landed — pinning a release
    whose changes were thrown away. The abort closes that by clearing the
    record while HEAD is still on the pre-update commit.
    """
    pinned = basepin.read_base(box)
    _self_edit(box, box / "src/chief/local.py", "chief wrote this\n")
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    assert update(repo_dir=box, say=lambda _: None) == 0
    assert basepin.read_pending(box) is not None

    said: list[str] = []
    assert abort_update(repo_dir=box, say=said.append) == 0
    # The apply is undone: upstream's change is gone, the self-edit stays, and
    # the tree is clean on the old version.
    assert (box / "README.md").read_text() == "chief\n"
    assert (box / "src/chief/local.py").read_text() == "chief wrote this\n"
    assert not git(box, "status", "--porcelain", "--untracked-files=no").strip()
    assert basepin.read_pending(box) is None

    # The mispin the record enabled can no longer happen: an unrelated self-edit
    # commits and a healthy boot resolves nothing, leaving the pin put.
    _self_edit(box, box / "src/chief/other.py", "unrelated later edit\n")
    assert basepin.resolve_pending(box) is None
    assert basepin.read_base(box) == pinned


def test_abort_without_a_pending_update_changes_nothing(box: Path) -> None:
    """No update in flight: abort must not touch a clean, legitimate tree."""
    _self_edit(box, box / "src/chief/local.py", "chief wrote this\n")
    before = git(box, "rev-parse", "HEAD").strip()
    said: list[str] = []
    assert abort_update(repo_dir=box, say=said.append) == 0
    assert git(box, "rev-parse", "HEAD").strip() == before
    assert (box / "src/chief/local.py").read_text() == "chief wrote this\n"
    assert any("nothing" in line.lower() for line in said)


def test_abort_command_reverts_and_clears(box: Path, upstream: Path) -> None:
    """The `--abort` flag is the shell-invocable half the skill calls."""
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    assert update(repo_dir=box, say=lambda _: None) == 0
    assert main(["update", "--repo", str(box), "--abort"]) == 0
    assert (box / "README.md").read_text() == "chief\n"
    assert basepin.read_pending(box) is None


def test_a_resolved_collision_commits_once_and_advances(
    box: Path, upstream: Path
) -> None:
    _self_edit(box, box / "src/chief/app.py", "line1\nLOCAL\nline3\n")
    release = _release(
        upstream, "0.4.0", upstream / "src/chief/app.py", "line1\nUPSTREAM\nline3\n"
    )
    before = git(box, "rev-parse", "HEAD").strip()
    assert update(repo_dir=box, say=lambda _: None) == layer.CONFLICTS_EXIT
    # The model's resolution, supplied directly against the real conflicted tree.
    _write(box / "src/chief/app.py", "line1\nUPSTREAM\nLOCAL\nline3\n")
    _commit(box, "update to v0.4.0")
    assert git(box, "rev-list", "--count", f"{before}..HEAD").strip() == "1"
    assert basepin.resolve_pending(box) == "v0.4.0"
    assert basepin.read_base(box) == release


# --- cutting a release ------------------------------------------------------


def _push_target(tmp_path: Path, upstream: Path) -> Path:
    """Give the upstream repo a real remote to push into."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(upstream, "remote", "add", "origin", str(remote))
    git(upstream, "push", "-q", "-u", "origin", "main")
    return remote


def test_release_bumps_tags_and_pushes(tmp_path: Path, upstream: Path) -> None:
    remote = _push_target(tmp_path, upstream)
    published: list[str] = []
    said: list[str] = []
    assert (
        cut_release(
            repo_dir=upstream,
            part="minor",
            checks=(("true",),),
            publish=lambda tag, notes: published.append(tag),
            say=said.append,
        )
        == 0
    )
    assert releases.read_project_version(upstream / "pyproject.toml") == (
        releases.Version(0, 4, 0)
    )
    assert git(upstream, "rev-parse", "v0.4.0^{commit}").strip()
    assert "v0.4.0" in git(remote, "tag", "--list")
    assert published == ["v0.4.0"]
    assert not git(upstream, "status", "--porcelain").strip()


def test_release_refuses_a_dirty_tree(tmp_path: Path, upstream: Path) -> None:
    _push_target(tmp_path, upstream)
    (upstream / "README.md").write_text("uncommitted\n")
    said: list[str] = []
    assert (
        cut_release(
            repo_dir=upstream,
            part="patch",
            checks=(("true",),),
            publish=lambda tag, notes: None,
            say=said.append,
        )
        == 1
    )
    assert not git(upstream, "tag", "--list", "v0.3.1").strip()
    assert any("uncommitted" in line for line in said)


def test_release_refuses_a_red_done_check(tmp_path: Path, upstream: Path) -> None:
    _push_target(tmp_path, upstream)
    said: list[str] = []
    assert (
        cut_release(
            repo_dir=upstream,
            part="patch",
            checks=(("false",),),
            publish=lambda tag, notes: None,
            say=said.append,
        )
        == 1
    )
    assert not git(upstream, "tag", "--list", "v0.3.1").strip()
    assert releases.read_project_version(upstream / "pyproject.toml") == (
        releases.Version(0, 3, 0)
    )
    assert any("done-check" in line for line in said)


def test_release_notes_list_the_commits_since_the_last_tag(
    tmp_path: Path, upstream: Path
) -> None:
    _push_target(tmp_path, upstream)
    _write(upstream / "src/chief/new.py", "feature\n")
    _commit(upstream, "feat: a new thing")
    notes: list[str] = []
    cut_release(
        repo_dir=upstream,
        part="patch",
        checks=(("true",),),
        publish=lambda tag, body: notes.append(body),
        say=lambda _: None,
    )
    assert "feat: a new thing" in notes[0]


# --- the skills eviction migration ------------------------------------------


def test_migration_untracks_installed_skills_without_losing_content(
    box: Path,
) -> None:
    """The one-time crossover: skills/ becomes instance data, contents intact."""
    _write(box / "skills/screening/SKILL.md", "chief's own edit\n")
    _write(box / "core-skills/self-edit/SKILL.md", "---\nname: self-edit\n---\nbody\n")
    _commit(box, "instance state: an installed skill")
    assert "skills/screening/SKILL.md" in git(box, "ls-files", "skills")
    migrate.migrate_instance(box)
    assert not git(box, "ls-files", "skills").strip()  # untracked now
    assert (box / "skills/screening/SKILL.md").read_text() == "chief's own edit\n"
    assert not git(box, "status", "--porcelain", "--untracked-files=no").strip()
    assert "/skills/" in (box / ".gitignore").read_text()


def test_a_failed_untrack_commit_leaves_a_clean_tree(box: Path) -> None:
    """If the crossover commit fails, nothing may be left staged.

    A leftover staged ``rm`` + .gitignore edit would keep the tree dirty, and
    the boot guard would then skip the whole migration forever — the base pin
    would never be recorded. A blocking pre-commit hook forces the failure.
    """
    _write(box / "skills/screening/SKILL.md", "chief's own edit\n")
    _commit(box, "instance state: an installed skill")
    original_ignore = (box / ".gitignore").read_text()
    hook = box / ".git/hooks/pre-commit"
    _write(hook, "#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    migrate.migrate_instance(box)  # fail-soft: logs and skips

    assert not git(box, "status", "--porcelain", "--untracked-files=no").strip()
    assert "skills/screening/SKILL.md" in git(box, "ls-files", "skills")
    assert (box / ".gitignore").read_text() == original_ignore


def test_migration_seeds_core_skills_that_are_missing(box: Path) -> None:
    _write(box / "core-skills/self-edit/SKILL.md", "---\nname: self-edit\n---\nbody\n")
    _write(box / "skills/self-edit/SKILL.md", "chief's own edit\n")
    _commit(box, "core skills")
    migrate.migrate_instance(box)
    # An existing installed copy is never clobbered by the seed...
    assert (box / "skills/self-edit/SKILL.md").read_text() == "chief's own edit\n"
    # ...but a missing one is restored.
    (box / "skills/self-edit/SKILL.md").unlink()
    migrate.migrate_instance(box)
    assert "name: self-edit" in (box / "skills/self-edit/SKILL.md").read_text()


def test_migration_records_the_base_pin_at_the_release_the_box_is_on(
    box: Path,
) -> None:
    git(box, "update-ref", "-d", basepin.BASE_REF)
    _self_edit(box, box / "src/chief/local.py", "chief wrote this\n")
    migrate.migrate_instance(box)
    assert basepin.read_base(box) == git(box, "rev-parse", "v0.3.0^{commit}").strip()


def test_migration_is_idempotent(box: Path) -> None:
    _write(box / "core-skills/self-edit/SKILL.md", "---\nname: self-edit\n---\nbody\n")
    _commit(box, "core skills")
    first = migrate.migrate_instance(box)
    assert first  # something was done
    assert not migrate.migrate_instance(box)  # and nothing is left to do
    assert not git(box, "status", "--porcelain", "--untracked-files=no").strip()


def test_migration_leaves_the_pin_alone_when_already_set(box: Path) -> None:
    pinned = basepin.read_base(box)
    _self_edit(box, box / "src/chief/local.py", "x\n")
    migrate.migrate_instance(box)
    assert basepin.read_base(box) == pinned


def test_an_unknown_commit_never_wipes_the_pin(box: Path) -> None:
    """`git update-ref` reads a bad object as *delete*, and a box with no pin
    cannot update itself at all."""
    pinned = basepin.read_base(box)
    assert basepin.write_base(box, "0" * 40) is False
    assert basepin.read_base(box) == pinned


# --- the cached "is there a newer release?" verdict -------------------------


def test_check_reports_the_pinned_release_and_the_newest_one(
    box: Path, upstream: Path
) -> None:
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    status = updatecheck.refresh(box, status_path=box / "data/update_status.json")
    assert status is not None
    assert (status.current, status.latest) == ("v0.3.0", "v0.4.0")
    assert status.behind


def test_check_is_not_behind_at_the_newest_release(box: Path) -> None:
    status = updatecheck.refresh(box, status_path=box / "data/update_status.json")
    assert status is not None
    assert (status.current, status.latest) == ("v0.3.0", "v0.3.0")
    assert not status.behind


def test_check_survives_a_pin_that_is_not_a_release_commit(
    box: Path, upstream: Path
) -> None:
    """A box pinned at a shared ancestor still gets told a release is out."""
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    basepin.write_base(box, _self_edit(box, box / "src/chief/local.py", "x\n"))
    status = updatecheck.refresh(box, status_path=box / "data/update_status.json")
    assert status is not None and status.current == ""
    assert status.behind


def test_check_updates_command_reports_without_changing_anything(
    box: Path, upstream: Path, capsys: "pytest.CaptureFixture[str]"
) -> None:
    _release(upstream, "0.4.0", upstream / "README.md", "four\n")
    before = git(box, "rev-parse", "HEAD").strip()
    assert main(["check-updates", "--repo", str(box)]) == 0
    assert "v0.4.0" in capsys.readouterr().out
    assert git(box, "rev-parse", "HEAD").strip() == before
    assert not git(box, "status", "--porcelain", "--untracked-files=no").strip()


def test_pending_record_is_json_a_human_can_read(box: Path) -> None:
    basepin.record_pending(box, commit="abc", version="v9.9.9", head="def")
    raw = json.loads((box / basepin.PENDING_NAME).read_text())
    assert raw == {"commit": "abc", "version": "v9.9.9", "head": "def"}
