"""Worktree namespacing, change detection and write-set enforcement."""

from __future__ import annotations

import pytest

from orchestrator import gitio
from orchestrator.errors import OrchestratorError, Reason
from orchestrator.task_loader import load_tasks
from orchestrator.verifier import audit_write_set, verify
from orchestrator.worktrees import WorktreeManager
from tests.conftest import git, write


def _worktrees(config):
    manager = WorktreeManager(config)
    police = manager.create(config.repo("police"), "T002")
    thief = manager.create(config.repo("thief"), "T002")
    return manager, police, thief


def test_police_and_thief_t002_never_collide(config):
    _, police, thief = _worktrees(config)
    assert police.path != thief.path
    assert police.path.endswith("worktrees/police/T002")
    assert thief.path.endswith("worktrees/thief/T002")
    assert police.repo_identity != thief.repo_identity


def test_worktree_records_its_provenance(config):
    manager, police, _ = _worktrees(config)
    assert police.base_sha == gitio.rev_parse(config.repo("police").path, "origin/master")
    assert police.branch == "task/T002"
    assert police.base_ref == "origin/master"
    assert police.created_at
    assert manager.load(config.repo("police"), "T002") == police


def test_stale_base_is_reported_not_silently_reused(config):
    manager, police, _ = _worktrees(config)
    repo = config.repo("police")
    write(repo.path, "docs/TODO.md", "# TODO\n\nmoved on\n")
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "master moves on"], repo.path)
    git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo.path)

    assert "predates" in manager.staleness(repo, police)
    with pytest.raises(OrchestratorError) as excinfo:
        manager.require_fresh(repo, "T002")
    assert excinfo.value.refusal.reason is Reason.WORKTREE_STALE_BASE


def test_agent_metadata_lives_outside_the_worktree(config):
    manager, _police, _ = _worktrees(config)
    agent_dir = manager.agent_dir(config.repo("police"), "T002")
    assert config.workspace in agent_dir.parents
    assert str(agent_dir).startswith(str(config.state_dir))
    # nothing under the worktree, so nothing can be committed as product code
    assert not (config.workspace / "worktrees" / "police" / "T002" / ".agent").exists()


# ------------------------------------------------------------ change detection


def _tree(config, tmp_path):
    _manager, police, _ = _worktrees(config)
    from pathlib import Path

    return Path(police.path), police.base_sha


def test_untracked_out_of_scope_file_is_detected(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    write(tree, "sneaky.py", "print('not mine')\n")
    _, refusals = audit_write_set(tree, base, ["pyproject.toml", "uv.lock"])
    assert [r.reason for r in refusals] == [Reason.OUT_OF_WRITE_SET]
    assert refusals[0].detail["path"] == "sneaky.py"
    assert refusals[0].detail["origin"] == "untracked"


def test_committed_out_of_scope_change_is_detected(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    write(tree, "docs/PRD.md", "# PRD\n\nquietly rewritten\n")
    git(["add", "-A"], tree)
    git(["commit", "-q", "-m", "sneak"], tree)
    _, refusals = audit_write_set(tree, base, ["pyproject.toml"])
    assert any(r.detail["path"] == "docs/PRD.md" for r in refusals)
    assert any(r.detail["origin"] == "committed" for r in refusals)


def test_staged_out_of_scope_change_is_detected(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    write(tree, "docs/PLAN.md", "# PLAN\n\nstaged only\n")
    git(["add", "docs/PLAN.md"], tree)
    _, refusals = audit_write_set(tree, base, ["pyproject.toml"])
    assert any(r.detail["path"] == "docs/PLAN.md" for r in refusals)


def test_deletion_out_of_scope_is_detected(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    (tree / "docs" / "TODO.md").unlink()
    _, refusals = audit_write_set(tree, base, ["pyproject.toml"])
    assert any(r.detail["path"] == "docs/TODO.md" for r in refusals)
    assert any(r.detail["kind"] == "DELETED" for r in refusals)


def test_rename_is_audited_on_both_paths(config, tmp_path):
    """Moving a file out of the write set mutates a path the task does not own."""
    tree, base = _tree(config, tmp_path)
    git(["mv", "pyproject.toml", "moved.toml"], tree)
    git(["commit", "-q", "-m", "rename"], tree)
    _, refusals = audit_write_set(tree, base, ["pyproject.toml"])
    paths = {r.detail["path"] for r in refusals}
    assert "moved.toml" in paths          # new path is outside the write set
    assert "pyproject.toml" not in paths  # old path is owned, so not a violation


def test_in_scope_changes_produce_no_refusal(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    write(tree, "pyproject.toml", "[project]\nname = 'changed'\n")
    write(tree, "uv.lock", "# lock\n")
    _, refusals = audit_write_set(tree, base, ["pyproject.toml", "uv.lock"])
    assert refusals == []


def test_agent_prefix_is_ignored_in_audits(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    write(tree, ".agent/notes.md", "runtime scratch\n")
    _, refusals = audit_write_set(tree, base, ["pyproject.toml"])
    assert refusals == []


def test_paths_with_spaces_survive_parsing(config, tmp_path):
    """v3 split git output on whitespace and mangled exactly this case."""
    tree, base = _tree(config, tmp_path)
    write(tree, "a file with spaces.txt", "x\n")
    _, refusals = audit_write_set(tree, base, ["pyproject.toml"])
    assert [r.detail["path"] for r in refusals] == ["a file with spaces.txt"]


# ----------------------------------------------------------------- verification


def test_verification_failure_is_decided_by_exit_code(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    report = verify("police", "T002", tree, base, ["pyproject.toml"], ["false"])
    assert report.passed is False
    assert report.commands[0].exit_code != 0


def test_verification_side_effect_outside_write_set_is_reported(config, tmp_path):
    """A test run that drops a file outside the write set must not pass silently."""
    tree, base = _tree(config, tmp_path)
    report = verify(
        "police", "T002", tree, base, ["pyproject.toml"], ["touch generated_artifact.tmp"]
    )
    assert report.commands[0].passed is True
    assert report.passed is False
    assert [r.reason for r in report.side_effects] == [Reason.VERIFICATION_SIDE_EFFECT]
    assert (tree / "generated_artifact.tmp").exists(), "report first, never delete"


def test_clean_verification_passes(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    report = verify("police", "T002", tree, base, ["pyproject.toml"], ["true"])
    assert report.passed is True


def test_verification_with_no_commands_does_not_pass(config, tmp_path):
    tree, base = _tree(config, tmp_path)
    assert verify("police", "T002", tree, base, ["pyproject.toml"], []).passed is False


def test_task_verification_commands_are_taken_from_the_task(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    assert tasks["T002"].verification_commands == ["true"]
