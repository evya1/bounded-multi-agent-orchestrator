"""Claiming preconditions and the refusal to ship without proof."""

from __future__ import annotations

from pathlib import Path

from orchestrator import gitio
from orchestrator.approvals import ApprovalStore
from orchestrator.claiming import evaluate_claim
from orchestrator.cli import main
from orchestrator.errors import Reason
from orchestrator.registers import Registers
from orchestrator.state import State, StateStore
from orchestrator.task_loader import load_tasks
from orchestrator.verifier import IGNORED_PREFIXES
from orchestrator.worktrees import WorktreeManager
from tests.conftest import git, make_task, write


def _worktree(config, repo_name, task_id="T002"):
    """Every model stage requires an explicit task worktree — create one."""
    manager = WorktreeManager(config)
    repo = config.repo(repo_name)
    return manager.load(repo, task_id) or manager.create(repo, task_id)


def _claim(config, repo_name, stage="plan", plan_text=None, task_id="T002", worktree=True):
    repo = config.repo(repo_name)
    if worktree:
        _worktree(config, repo_name, task_id)
    tasks = load_tasks(repo.path, config.project.task_dirs)
    return evaluate_claim(
        config, repo, tasks[task_id], tasks,
        Registers(repo.path, config.project), StateStore(config), ApprovalStore(config),
        gitio.rev_parse(repo.path, "origin/master"), stage, plan_text,
    )


def test_a_ready_task_with_compilable_context_may_be_claimed(config):
    decision = _claim(config, "police")
    assert decision.allowed is True
    assert decision.manifest is not None
    assert set(decision.resources) == {"police:dependency_manifest", "police:dependency_lock"}


def test_claim_refuses_when_the_bounded_context_cannot_compile(config):
    """The context that matters is the WORKTREE's, not the main checkout's."""
    record = _worktree(config, "police")
    (Path(record.path) / "docs" / "PRD.md").unlink()
    decision = _claim(config, "police")
    assert decision.allowed is False
    assert Reason.CONTEXT_COMPILE_FAILED in [r.reason for r in decision.refusals]


def test_claim_refuses_when_a_repository_resource_is_held(config):
    """T002 owns pyproject.toml/uv.lock; a concurrent holder serializes it out."""
    store = StateStore(config)
    repo = config.repo("police")
    other = store.get_or_create(repo, "T030")
    other.resources = ["police:dependency_lock"]
    store.save(repo, other)
    store.transition(repo, other, State.PLANNED)

    decision = _claim(config, "police")
    assert decision.allowed is False
    held = [r for r in decision.refusals if r.reason is Reason.RESOURCE_HELD]
    assert held and held[0].detail["holders"] == ["T030"]


def test_thief_may_hold_its_own_dependency_lock_concurrently(config):
    """Police and Thief are independent repositories."""
    store = StateStore(config)
    police = config.repo("police")
    holder = store.get_or_create(police, "T030")
    holder.resources = ["police:dependency_lock"]
    store.save(police, holder)
    store.transition(police, holder, State.PLANNED)

    assert _claim(config, "police").allowed is False
    assert _claim(config, "thief").allowed is True


def test_claim_refuses_a_write_conflict_with_an_active_task(config, tmp_path):
    repo = config.repo("police")
    write(repo.path, "docs/tasks/T030-fixture.md", make_task(task_id="T030", write_set=("pyproject.toml",)))
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "add T030"], repo.path)

    store = StateStore(config)
    other = store.get_or_create(repo, "T030")
    store.save(repo, other)
    store.transition(repo, other, State.PLANNED)

    decision = _claim(config, "police")
    assert Reason.WRITE_CONFLICT in [r.reason for r in decision.refusals]


def test_high_risk_implement_requires_an_approved_plan(config, tmp_path):
    repo = config.repo("police")
    write(repo.path, "docs/tasks/T002-fixture.md", make_task(risk="high"))
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "raise risk"], repo.path)

    decision = _claim(config, "police", stage="implement", plan_text="a plan")
    assert decision.allowed is False
    assert Reason.APPROVAL_MISSING in [r.reason for r in decision.refusals]


def test_medium_risk_implement_needs_no_plan_gate(config):
    assert _claim(config, "police", stage="implement", plan_text="a plan").allowed is True


def test_approved_plan_unblocks_a_high_risk_implement(config, tmp_path):
    repo = config.repo("police")
    write(repo.path, "docs/tasks/T002-fixture.md", make_task(risk="high"))
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "raise risk"], repo.path)
    base = gitio.rev_parse(repo.path, "origin/master")
    ApprovalStore(config).record(repo, "T002", "plan", base, "a plan", True)

    assert _claim(config, "police", stage="implement", plan_text="a plan").allowed is True


def test_a_changed_plan_re_blocks_a_high_risk_implement(config, tmp_path):
    repo = config.repo("police")
    write(repo.path, "docs/tasks/T002-fixture.md", make_task(risk="high"))
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "raise risk"], repo.path)
    base = gitio.rev_parse(repo.path, "origin/master")
    ApprovalStore(config).record(repo, "T002", "plan", base, "a plan", True)

    decision = _claim(config, "police", stage="implement", plan_text="a plan, edited")
    assert Reason.APPROVAL_ARTIFACT_MISMATCH in [r.reason for r in decision.refusals]


# ------------------------------------------------------------------ shipping


RESOLVED_CRITERION = {"id": "PLANQ-009", "kind": "decision", "scope": "dependency_lock", "blocks": "criterion"}


def _prepared(config, monkeypatch, risk="medium", edit=("pyproject.toml",), gates=None):
    """A worktree with in-scope changes, verified, ready for a ship attempt."""
    monkeypatch.setattr("orchestrator.config.load_config", lambda *a, **k: config)
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    repo = config.repo("police")
    if gates is not None:
        write(repo.path, "docs/tasks/T002-fixture.md", make_task(risk=risk, gates=gates))
        git(["add", "-A"], repo.path)
        git(["commit", "-q", "-m", "set gates"], repo.path)
        git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo.path)
    elif risk != "medium":
        write(repo.path, "docs/tasks/T002-fixture.md", make_task(risk=risk))
        git(["add", "-A"], repo.path)
        git(["commit", "-q", "-m", "set risk"], repo.path)
        git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo.path)
    record = WorktreeManager(config).create(repo, "T002")
    tree = Path(record.path)
    for relative in edit:
        write(tree, relative, "# changed by the fixture\n")
    return repo, tree, record


def test_ship_refuses_before_verification(config, monkeypatch, capsys):
    _prepared(config, monkeypatch)
    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    assert code == 2
    assert Reason.VERIFICATION_NOT_RUN in capsys.readouterr().out


def test_ship_refuses_a_medium_risk_diff_without_human_approval(config, monkeypatch, capsys):
    repo, _tree, _record = _prepared(config, monkeypatch)
    store = StateStore(config)
    state = store.get_or_create(repo, "T002")
    store.transition(repo, state, State.PLANNED)
    store.transition(repo, state, State.IMPLEMENTED)
    store.transition(repo, state, State.VERIFIED)

    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    assert code == 2
    assert "APPROVAL_MISSING" in capsys.readouterr().out


def test_ship_refuses_an_out_of_write_set_change(config, monkeypatch, capsys):
    repo, tree, _record = _prepared(config, monkeypatch)
    write(tree, "docs/PLAN.md", "# PLAN\n\nquietly rewritten\n")
    store = StateStore(config)
    state = store.get_or_create(repo, "T002")
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED):
        store.transition(repo, state, target)

    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    assert code == 2
    output = capsys.readouterr().out
    assert "OUT_OF_WRITE_SET" in output and "docs/PLAN.md" in output


def test_ship_proceeds_once_verified_and_approved(config, monkeypatch, capsys):
    repo, tree, record = _prepared(config, monkeypatch, gates=(RESOLVED_CRITERION,))
    store = StateStore(config)
    state = store.get_or_create(repo, "T002")
    state.provenance = {"implemented_by": "qwen-local"}
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED):
        store.transition(repo, state, target)

    artifact = gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES)
    ApprovalStore(config).record(repo, "T002", "diff", record.base_sha, artifact, True, "reviewed")

    assert main(["pr", "--repo", "police", "--task", "T002", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Reviewed-By: none" in output          # no model review actually ran
    assert "Human-Approved: true" in output
    assert "Implemented-By: qwen-local" in output


def test_editing_the_diff_after_approval_re_blocks_the_ship(config, monkeypatch, capsys):
    repo, tree, record = _prepared(config, monkeypatch)
    store = StateStore(config)
    state = store.get_or_create(repo, "T002")
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED):
        store.transition(repo, state, target)
    artifact = gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES)
    ApprovalStore(config).record(repo, "T002", "diff", record.base_sha, artifact, True)

    write(tree, "pyproject.toml", "# changed AFTER the human approved\n")
    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    assert code == 2
    assert "APPROVAL_ARTIFACT_MISMATCH" in capsys.readouterr().out


def test_governance_diff_always_requires_approval_even_at_low_risk(config, monkeypatch, capsys):
    repo, tree, _record = _prepared(
        config, monkeypatch, risk="low", edit=("pyproject.toml",)
    )
    # the task's write_set legitimately grows to include a governance path
    write(repo.path, "docs/tasks/T002-fixture.md",
          make_task(risk="low", write_set=("pyproject.toml", "docs/PRD.md")))
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "own PRD"], repo.path)
    write(tree, "docs/PRD.md", "# PRD\n\nedited by the task that owns it\n")

    store = StateStore(config)
    state = store.get_or_create(repo, "T002")
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED):
        store.transition(repo, state, target)

    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    assert code == 2
    output = capsys.readouterr().out
    assert "governance" in output
    assert "APPROVAL_MISSING" in output


def test_cli_context_command_runs_read_only(config, monkeypatch, capsys):
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    assert main(["context", "--repo", "police", "--task", "T002"]) == 0
    assert "BOUNDED CONTEXT" in capsys.readouterr().out


def test_cli_status_and_queue(config, monkeypatch, capsys):
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    assert main(["status"]) == 0
    assert main(["queue"]) == 0
    assert "T002" in capsys.readouterr().out


def test_cli_budget_reports_provenance(config, monkeypatch, capsys):
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    assert main(["budget"]) == 0
    output = capsys.readouterr().out
    assert "REPORTED" in output and "ESTIMATED" in output and "Neither is an invoice" in output
