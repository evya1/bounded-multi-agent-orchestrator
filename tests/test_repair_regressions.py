"""Regressions for the independent review's findings.

Each test names the defect it pins down. They use throwaway fixture repos and a
fake model runner: nothing here touches Police, Thief, the Bundle, or a paid API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import gitio, prompts, readiness
from orchestrator.adapters import RunResult
from orchestrator.approvals import ApprovalStore
from orchestrator.cli import main
from orchestrator.context_compiler import ContextCompiler, Kind
from orchestrator.errors import Reason
from orchestrator.registers import Registers
from orchestrator.state import State, StateStore
from orchestrator.task_loader import load_tasks
from orchestrator.verifier import IGNORED_PREFIXES
from orchestrator.worktrees import WorktreeManager
from tests.conftest import git, make_task, write

CRITERION_GATE = {"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "criterion"}
INTEGRATION_GATE = {"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "integration"}
RESOLVED_GATE = {"id": "PLANQ-009", "kind": "decision", "scope": "dependency_lock", "blocks": "criterion"}


@pytest.fixture
def cli(config, monkeypatch):
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    return config


def _snapshot(repo_path: Path) -> tuple[str, str]:
    return (
        gitio.git(["rev-parse", "HEAD"], repo_path).strip(),
        gitio.git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], repo_path),
    )


def _set_task(config, body: str, repo_name: str = "police") -> None:
    repo = config.repo(repo_name)
    write(repo.path, "docs/tasks/T002-fixture.md", body)
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "reshape task"], repo.path)
    git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo.path)


# =====================================================================
# §1  A mutating stage must never fall back to the role repository
# =====================================================================


@pytest.mark.parametrize("stage", ["implement", "review", "verify"])
def test_stage_without_a_worktree_refuses(cli, capsys, stage):
    config = cli
    before = _snapshot(config.repo("police").path)

    code = main([stage, "--repo", "police", "--task", "T002", *(["--adapter", "fake"] if stage != "verify" else [])])

    assert code == 2, f"{stage} must refuse without a worktree"
    assert "WORKTREE_MISSING" in capsys.readouterr().out
    assert _snapshot(config.repo("police").path) == before, "the role repo must be untouched"


def test_plan_also_requires_a_worktree(cli, capsys):
    """One invariant for every model stage: no stage has a repo.path fallback."""
    config = cli
    before = _snapshot(config.repo("police").path)
    code = main(["plan", "--repo", "police", "--task", "T002", "--adapter", "fake"])
    assert code == 2
    assert "WORKTREE_MISSING" in capsys.readouterr().out
    assert _snapshot(config.repo("police").path) == before


def test_no_stage_can_resolve_its_workspace_to_the_role_repo(config):
    """The fallback that made this possible is gone: workspace() now refuses."""
    from orchestrator.cli import Session
    from orchestrator.errors import OrchestratorError

    session = Session(config, "police")
    with pytest.raises(OrchestratorError) as excinfo:
        session.workspace("T002")
    assert excinfo.value.refusal.reason is Reason.WORKTREE_MISSING


def test_stage_refuses_on_a_stale_worktree(cli, capsys):
    config = cli
    WorktreeManager(config).create(config.repo("police"), "T002")
    write(config.repo("police").path, "docs/TODO.md", "# TODO\n\nmaster moved\n")
    git(["add", "-A"], config.repo("police").path)
    git(["commit", "-q", "-m", "master moves"], config.repo("police").path)
    git(["update-ref", "refs/remotes/origin/master", "HEAD"], config.repo("police").path)

    code = main(["implement", "--repo", "police", "--task", "T002", "--adapter", "fake"])
    assert code == 2
    assert "WORKTREE_STALE_BASE" in capsys.readouterr().out


def test_diff_approval_requires_a_worktree(cli, capsys):
    code = main(["approve", "--repo", "police", "--task", "T002", "--stage", "diff"])
    assert code == 2
    assert "WORKTREE_MISSING" in capsys.readouterr().out


# =====================================================================
# §3  The reviewer must receive the real change, not hashes
# =====================================================================


def _reviewable(config, monkeypatch):
    """A worktree carrying a meaningful, in-scope source change."""
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("orchestrator.model_router._probe_http", lambda url, timeout=2.0: (True, "HTTP 200"))
    _set_task(config, make_task(write_set=("pyproject.toml", "tests/")))
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    write(tree, "pyproject.toml", "[project]\nname = 'police'\nrequires-python = '>=3.12'\n")
    write(tree, "tests/test_runtime_dependencies.py", "def test_fastmcp_imports():\n    import fastmcp\n")
    return tree, record


def test_reviewer_prompt_contains_the_actual_code(config, monkeypatch, capsys):
    _reviewable(config, monkeypatch)
    monkeypatch.setenv("FIXTURE_KEY", "sentinel")
    monkeypatch.setenv("FIXTURE_LOCAL_URL", "http://127.0.0.1:9")

    assert main(["review", "--repo", "police", "--task", "T002", "--adapter", "fake", "--dry-run"]) == 0

    prompt = (config.state_dir / "agent" / "police" / "T002" / "T002-review-prompt.md").read_text()
    # the real changed code, not a digest
    assert "requires-python = '>=3.12'" in prompt
    assert "def test_fastmcp_imports():" in prompt
    assert "import fastmcp" in prompt
    # structural information a reviewer needs
    assert "UNIFIED DIFF vs base" in prompt or "NEW FILE" in prompt
    assert "CHANGE SUMMARY" in prompt


def test_reviewer_prompt_excludes_unrelated_repository_content(config, monkeypatch):
    _reviewable(config, monkeypatch)
    monkeypatch.setenv("FIXTURE_KEY", "sentinel")
    main(["review", "--repo", "police", "--task", "T002", "--adapter", "fake", "--dry-run"])
    prompt = (config.state_dir / "agent" / "police" / "T002" / "T002-review-prompt.md").read_text()

    # docs/components/C01 is in the repo but is not part of the change set,
    # and is not declared context for this task
    assert "# C01 PRD" not in prompt
    assert "Unrelated decision" not in prompt          # ADR-009 body
    assert "The board MUST be square" not in prompt    # GAME-001 register row


def test_review_patch_reports_deletions_and_renames(config):
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    git(["mv", "pyproject.toml", "renamed.toml"], tree)
    (tree / "docs" / "TODO.md").unlink()
    patch = gitio.review_patch(tree, record.base_sha, IGNORED_PREFIXES)
    assert "renamed.toml" in patch
    assert "docs/TODO.md" in patch
    assert "CHANGE SUMMARY" in patch


def test_review_patch_inlines_new_untracked_files(config):
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    write(tree, "tests/test_new.py", "assert True  # brand new file\n")
    patch = gitio.review_patch(tree, record.base_sha, IGNORED_PREFIXES)
    assert "NEW FILE (untracked): tests/test_new.py" in patch
    assert "brand new file" in patch


def test_change_manifest_is_still_hashes_for_approval_identity(config):
    """The manifest keeps its job; the patch is a separate artifact."""
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    write(tree, "pyproject.toml", "[project]\nname='x'\n")
    manifest = gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES)
    assert "pyproject.toml\t100644\t" in manifest
    assert "[project]" not in manifest


# =====================================================================
# §4  A failed or escalating model call must never advance state
# =====================================================================


def _ready_worktree(config, monkeypatch, risk="medium"):
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config)
    monkeypatch.setattr("orchestrator.model_router._probe_http", lambda url, timeout=2.0: (True, "HTTP 200"))
    monkeypatch.setenv("FIXTURE_KEY", "sentinel")
    monkeypatch.setenv("FIXTURE_LOCAL_URL", "http://127.0.0.1:9")
    if risk != "medium":
        _set_task(config, make_task(risk=risk))
    return WorktreeManager(config).create(config.repo("police"), "T002")


def _fake_result(monkeypatch, result: RunResult):
    from orchestrator.adapters import FakeRunner

    runner = FakeRunner(result)
    monkeypatch.setattr("orchestrator.cli.build_adapter", lambda name, spec=None: runner)
    return runner


@pytest.mark.parametrize(
    ("stage", "forbidden_state"),
    [("plan", State.PLANNED), ("implement", State.IMPLEMENTED), ("review", State.REVIEWED)],
)
def test_failed_model_call_does_not_advance_state(config, monkeypatch, stage, forbidden_state):
    _ready_worktree(config, monkeypatch)
    _fake_result(monkeypatch, RunResult(exit_code=3, text="provider exploded"))

    code = main([stage, "--repo", "police", "--task", "T002", "--adapter", "fake", "--yes-spend"])

    assert code == 1, "a failed model call reports failure"
    state = StateStore(config).load(config.repo("police"), "T002")
    assert state is not None
    assert state.current is not forbidden_state
    assert state.current is State.READY


def test_failed_review_records_no_reviewer_provenance(config, monkeypatch):
    _ready_worktree(config, monkeypatch)
    _fake_result(monkeypatch, RunResult(exit_code=2, text="reviewer crashed\nAPPROVE"))

    main(["review", "--repo", "police", "--task", "T002", "--adapter", "fake", "--yes-spend"])

    state = StateStore(config).load(config.repo("police"), "T002")
    assert "reviewed_by" not in state.provenance
    assert "review_verdict" not in state.provenance


def test_failed_plan_writes_no_plan_artifact(config, monkeypatch):
    _ready_worktree(config, monkeypatch)
    _fake_result(monkeypatch, RunResult(exit_code=1, text="1. do the thing"))
    main(["plan", "--repo", "police", "--task", "T002", "--adapter", "fake", "--yes-spend"])
    assert not (config.state_dir / "agent" / "police" / "T002" / "T002-plan.md").exists()


def test_failed_call_still_records_cost_and_output(config, monkeypatch):
    """Tokens were spent and the diagnosis matters, even though the call failed."""
    from orchestrator.budget import Ledger

    _ready_worktree(config, monkeypatch)
    _fake_result(
        monkeypatch,
        RunResult(exit_code=1, text="boom", input_tokens=1000, output_tokens=10, reported_cost_usd=0.01),
    )
    main(["plan", "--repo", "police", "--task", "T002", "--adapter", "fake", "--yes-spend"])

    assert Ledger(config).entries()[-1].exit_code == 1
    output = config.state_dir / "agent" / "police" / "T002" / "T002-plan-output.md"
    assert output.read_text() == "boom"


def test_stop_needs_orchestrator_refuses_without_advancing(config, monkeypatch, capsys):
    _ready_worktree(config, monkeypatch)
    _fake_result(
        monkeypatch,
        RunResult(exit_code=0, text="STOP_NEEDS_ORCHESTRATOR\nWHAT IS MISSING: a decision"),
    )
    code = main(["implement", "--repo", "police", "--task", "T002", "--adapter", "fake", "--yes-spend"])

    assert code == 2
    assert "STOP_NEEDS_ORCHESTRATOR" in capsys.readouterr().out
    assert StateStore(config).load(config.repo("police"), "T002").current is State.READY


def test_successful_call_does_advance_state(config, monkeypatch):
    """The safety check must not have broken the happy path."""
    _ready_worktree(config, monkeypatch)
    _fake_result(monkeypatch, RunResult(exit_code=0, text="1. create pyproject.toml"))
    assert main(["plan", "--repo", "police", "--task", "T002", "--adapter", "fake", "--yes-spend"]) == 0
    assert StateStore(config).load(config.repo("police"), "T002").current is State.PLANNED


# =====================================================================
# §5  Post-start gate semantics
#
# blocks: start        -> the task cannot be claimed.
# blocks: criterion     -> the task may start; implementation, verification,
#                          review and a local candidate PR may all proceed;
#                          only the NAMED acceptance criterion may not be
#                          claimed satisfied, and the task may not be called
#                          done while it is outstanding.
# blocks: integration    -> local work and a candidate PR may proceed; only a
#                          claim that the named integration gate PASSED is
#                          illegitimate.
#
# A criterion/integration gate is therefore reported as PENDING STATUS, never
# as a blocker of claim/plan/implement/verify/review/PR_READY.
# =====================================================================


def _to_pr_ready(config, record, state) -> None:
    """Drive a prepared worktree through VERIFIED + an approved diff."""
    store = StateStore(config)
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED):
        store.transition(config.repo("police"), state, target)
    artifact = gitio.change_manifest(Path(record.path), record.base_sha, IGNORED_PREFIXES)
    ApprovalStore(config).record(config.repo("police"), "T002", "diff", record.base_sha, artifact, True)


def test_start_gate_still_refuses_the_claim(config):
    """§5 preserves the readiness rule exactly: unchanged from before this fix."""
    start_gate = {**CRITERION_GATE, "blocks": "start"}
    _set_task(config, make_task(gates=(start_gate,)))
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    registers = Registers(config.repo("police").path, config.project)
    verdict = readiness.evaluate(tasks["T002"], tasks, registers)
    assert verdict.ready is False
    assert [r.reason for r in verdict.refusals] == [Reason.START_GATE_UNRESOLVED]


def test_unresolved_criterion_gate_allows_the_full_local_pipeline(config, monkeypatch, capsys):
    """plan / implement / verify / review / PR_READY are all reachable."""
    config_ = config
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config_)

    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    registers = Registers(config.repo("police").path, config.project)
    assert readiness.evaluate(tasks["T002"], tasks, registers).ready is True

    record = WorktreeManager(config).create(config.repo("police"), "T002")
    write(Path(record.path), "pyproject.toml", "[project]\nname='x'\n")
    store = StateStore(config)
    state = store.get_or_create(config.repo("police"), "T002")
    _to_pr_ready(config, record, state)

    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    output = capsys.readouterr().out
    assert code == 0, "an unresolved criterion gate must NOT block a candidate PR"
    assert "PENDING PROJECT GATES" in output
    assert "PLANQ-002" in output
    assert "PR_READY != DONE" in output


def test_criterion_gate_is_reported_pending_never_satisfied(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    registers = Registers(config.repo("police").path, config.project)
    pending = readiness.criterion_pending(tasks["T002"], registers)
    assert [r.reason for r in pending] == [Reason.CRITERION_GATE_UNRESOLVED]
    assert "NOT satisfied" in pending[0].message
    assert "may not be marked done" in pending[0].message or "may not be closed" in pending[0].message


def test_resolved_criterion_gate_has_nothing_pending(config, monkeypatch, capsys):
    config_ = config
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config_)
    _set_task(config, make_task(gates=(RESOLVED_GATE,)))

    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    registers = Registers(config.repo("police").path, config.project)
    assert readiness.criterion_pending(tasks["T002"], registers) == []

    record = WorktreeManager(config).create(config.repo("police"), "T002")
    write(Path(record.path), "pyproject.toml", "[project]\nname='x'\n")
    state = StateStore(config).get_or_create(config.repo("police"), "T002")
    _to_pr_ready(config, record, state)

    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    output = capsys.readouterr().out
    assert code == 0
    assert "PENDING PROJECT GATES" not in output


def test_integration_gate_does_not_block_local_work_or_pr_ready(config, monkeypatch, capsys):
    config_ = config
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config_)
    _set_task(config, make_task(gates=(INTEGRATION_GATE,)))

    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    registers = Registers(config.repo("police").path, config.project)
    assert readiness.evaluate(tasks["T002"], tasks, registers).ready is True
    assert readiness.criterion_pending(tasks["T002"], registers) == []
    pending = readiness.integration_pending(tasks["T002"], registers)
    assert [r.reason for r in pending] == [Reason.INTEGRATION_GATE_PENDING]
    assert "NOT passed" in pending[0].message

    record = WorktreeManager(config).create(config.repo("police"), "T002")
    write(Path(record.path), "pyproject.toml", "[project]\nname='x'\n")
    state = StateStore(config).get_or_create(config.repo("police"), "T002")
    _to_pr_ready(config, record, state)

    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    output = capsys.readouterr().out
    assert code == 0
    assert "PENDING PROJECT GATES" in output
    assert "PLANQ-002" in output


def test_pr_ready_never_implies_done_even_with_criterion_and_integration_pending(config, monkeypatch, capsys):
    """Both kinds pending at once: PR_READY still reachable, nothing claimed resolved."""
    config_ = config
    monkeypatch.setattr("orchestrator.cli.load_config", lambda *a, **k: config_)
    both = (CRITERION_GATE, {**CRITERION_GATE, "id": "PLANQ-002", "blocks": "integration"})
    _set_task(config, make_task(gates=both))

    record = WorktreeManager(config).create(config.repo("police"), "T002")
    write(Path(record.path), "pyproject.toml", "[project]\nname='x'\n")
    state = StateStore(config).get_or_create(config.repo("police"), "T002")
    _to_pr_ready(config, record, state)

    # --dry-run: eligible-to-ship is proven (exit 0) without mutating git or
    # state — the assertions below target what dry-run actually promises.
    code = main(["pr", "--repo", "police", "--task", "T002", "--dry-run"])
    output = capsys.readouterr().out
    assert code == 0, "both kinds pending at once must still allow a candidate PR"
    assert "PENDING PROJECT GATES" in output
    assert output.count("PLANQ-002") >= 2  # both the criterion and the integration line
    assert "criterion:" in output and "integration:" in output
    assert "DONE" not in output.split("PENDING PROJECT GATES")[0]  # not claimed above the notice
    assert "PR_READY != DONE" in output


def test_start_gate_still_blocks_the_claim(config):
    """The readiness rule itself is unchanged."""
    _set_task(config, make_task(gates=({**CRITERION_GATE, "blocks": "start"},)))
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    registers = Registers(config.repo("police").path, config.project)
    verdict = readiness.evaluate(tasks["T002"], tasks, registers)
    assert verdict.ready is False
    assert [r.reason for r in verdict.refusals] == [Reason.START_GATE_UNRESOLVED]


# =====================================================================
# §6  Context must come from the task tree
# =====================================================================


def test_context_reflects_the_worktree_not_the_main_checkout(config):
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    write(tree, "pyproject.toml", "[project]\nname = 'edited-in-the-worktree'\n")
    write(config.repo("police").path, "pyproject.toml", "[project]\nname = 'main-checkout'\n")

    repo = config.repo("police")
    tasks = load_tasks(tree, config.project.task_dirs)
    compiler = ContextCompiler(config, repo, Registers(tree, config.project), tree=tree)
    manifest = compiler.compile(tasks["T002"], record.base_sha)

    owned = next(item for item in manifest.items if item.ref == "pyproject.toml")
    assert owned.kind is Kind.WRITE_OWNED
    assert "edited-in-the-worktree" in owned.text
    assert "main-checkout" not in owned.text
    # identity is still the repository, not the worktree
    assert manifest.repo_identity == repo.identity
    assert manifest.tree == str(tree)


def test_standalone_context_refuses_a_dirty_checkout(cli, capsys):
    config = cli
    write(config.repo("police").path, "stray.txt", "uncommitted\n")
    code = main(["context", "--repo", "police", "--task", "T002"])
    assert code == 2
    assert "TREE_NOT_PROVEN" in capsys.readouterr().out


def test_standalone_context_refuses_a_checkout_at_a_different_tree(cli, capsys):
    config = cli
    repo = config.repo("police")
    write(repo.path, "docs/TODO.md", "# TODO\n\nmoved past the base\n")
    git(["add", "-A"], repo.path)
    git(["commit", "-q", "-m", "move past base"], repo.path)
    code = main(["context", "--repo", "police", "--task", "T002"])
    assert code == 2
    assert "TREE_NOT_PROVEN" in capsys.readouterr().out


def test_standalone_context_allowed_on_a_proven_clean_base(cli, capsys):
    assert main(["context", "--repo", "police", "--task", "T002"]) == 0
    assert "BOUNDED CONTEXT" in capsys.readouterr().out


# =====================================================================
# §7  Approval artifacts cover Git-visible metadata, not just content
# =====================================================================


def test_executable_bit_flip_invalidates_an_approval(config):
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    target = tree / "pyproject.toml"
    write(tree, "pyproject.toml", "[project]\nname='x'\n")

    approved = gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES)
    store = ApprovalStore(config)
    store.record(config.repo("police"), "T002", "diff", record.base_sha, approved, True)
    assert store.check(config.repo("police"), "T002", "diff", record.base_sha, approved).valid

    target.chmod(0o755)  # content byte-identical, Git-visible mode changed
    current = gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES)
    assert current != approved
    check = store.check(config.repo("police"), "T002", "diff", record.base_sha, current)
    assert check.valid is False
    assert check.refusal.reason is Reason.APPROVAL_ARTIFACT_MISMATCH


def test_file_to_symlink_swap_invalidates_an_approval(config):
    record = WorktreeManager(config).create(config.repo("police"), "T002")
    tree = Path(record.path)
    write(tree, "pyproject.toml", "[project]\nname='x'\n")
    approved = gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES)

    (tree / "pyproject.toml").unlink()
    (tree / "pyproject.toml").symlink_to("/etc/hostname")
    assert gitio.change_manifest(tree, record.base_sha, IGNORED_PREFIXES) != approved


# =====================================================================
# Provenance still never overclaims
# =====================================================================


def test_commit_message_reports_none_when_no_review_ran(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    message = prompts.commit_message(tasks["T002"], "qwen-local", None, None, "bbb", True)
    assert "Reviewed-By: none" in message
