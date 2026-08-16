"""The exact readiness rule, and the runtime conflict checks around it."""

from __future__ import annotations

from orchestrator.config import ProjectConfig
from orchestrator.errors import Reason
from orchestrator.readiness import evaluate, write_conflicts
from orchestrator.registers import GateState, Registers
from orchestrator.task_loader import load_tasks, parse_task
from tests.conftest import build_repo, make_task, write

CRITERION_GATE = {"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "criterion"}
START_GATE = {"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "start"}
INTEGRATION_GATE = {"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "integration"}
UNKNOWN_START_GATE = {"id": "G-OFFICIAL", "kind": "input_gate", "scope": "official_schemas", "blocks": "start"}
RESOLVED_START_GATE = {"id": "PLANQ-009", "kind": "decision", "scope": "fmt", "blocks": "start"}


def _evaluate(tmp_path, repo_path, task_body, others=None):
    task = parse_task(write(tmp_path, "task.md", task_body))
    tasks = {task.id: task, **(others or {})}
    return evaluate(task, tasks, Registers(repo_path, ProjectConfig()))


def test_ready_when_no_dependencies_and_only_a_criterion_gate(tmp_path, repos):
    verdict = _evaluate(tmp_path, repos["police"], make_task(gates=(CRITERION_GATE,)))
    assert verdict.ready is True
    assert verdict.gate_verdicts[0].state is GateState.UNRESOLVED
    assert verdict.gate_verdicts[0].blocks_claim is False


def test_unresolved_integration_gate_does_not_block_start(tmp_path, repos):
    verdict = _evaluate(tmp_path, repos["police"], make_task(gates=(INTEGRATION_GATE,)))
    assert verdict.ready is True


def test_unresolved_start_gate_blocks(tmp_path, repos):
    verdict = _evaluate(tmp_path, repos["police"], make_task(gates=(START_GATE,)))
    assert verdict.ready is False
    assert [r.reason for r in verdict.refusals] == [Reason.START_GATE_UNRESOLVED]


def test_resolved_start_gate_does_not_block(tmp_path, repos):
    """PLANQ-009 carries a real Decision value, so its start gate is satisfied."""
    verdict = _evaluate(tmp_path, repos["police"], make_task(gates=(RESOLVED_START_GATE,)))
    assert verdict.gate_verdicts[0].state is GateState.RESOLVED
    assert verdict.ready is True


def test_unknown_start_gate_state_refuses_conservatively(tmp_path, repos):
    """A gate class has no mechanical marker — refuse rather than guess."""
    verdict = _evaluate(tmp_path, repos["police"], make_task(gates=(UNKNOWN_START_GATE,)))
    assert verdict.ready is False
    assert [r.reason for r in verdict.refusals] == [Reason.UNKNOWN_GATE_STATE]


def test_unknown_criterion_gate_state_does_not_block_start(tmp_path, repos):
    unknown_criterion = {**UNKNOWN_START_GATE, "blocks": "criterion"}
    verdict = _evaluate(tmp_path, repos["police"], make_task(gates=(unknown_criterion,)))
    assert verdict.gate_verdicts[0].state is GateState.UNKNOWN
    assert verdict.ready is True


def test_incomplete_dependency_blocks(tmp_path, repos):
    blocker = parse_task(write(tmp_path, "T001.md", make_task(task_id="T001", status="ready")))
    verdict = _evaluate(
        tmp_path,
        repos["police"],
        make_task(depends_on=("T001",), gates=(CRITERION_GATE,)),
        {"T001": blocker},
    )
    assert verdict.ready is False
    assert verdict.unmet_dependencies == ["T001"]
    assert [r.reason for r in verdict.refusals] == [Reason.DEPENDENCY_INCOMPLETE]


def test_done_dependency_unblocks(tmp_path, repos):
    done = parse_task(write(tmp_path, "T001.md", make_task(task_id="T001", status="done")))
    verdict = _evaluate(
        tmp_path,
        repos["police"],
        make_task(depends_on=("T001",), gates=(CRITERION_GATE,)),
        {"T001": done},
    )
    assert verdict.ready is True


def test_input_gate_resolution_reads_the_status_column(repos):
    registers = Registers(repos["police"], ProjectConfig())
    assert registers.gate_state("INPUT-001", "input")[0] is GateState.UNRESOLVED
    assert registers.gate_state("INPUT-009", "input")[0] is GateState.RESOLVED


def test_open_item_still_listed_is_unresolved(repos):
    registers = Registers(repos["police"], ProjectConfig())
    assert registers.gate_state("OPEN-001", "open")[0] is GateState.UNRESOLVED


# ------------------------------------------------------------ write conflicts


def test_overlapping_write_sets_between_active_tasks_conflict(tmp_path):
    mine = parse_task(write(tmp_path, "a.md", make_task(task_id="T002", write_set=("pyproject.toml",))))
    theirs = parse_task(write(tmp_path, "b.md", make_task(task_id="T005", write_set=("pyproject.toml",))))
    refusals = write_conflicts(mine, {"T005": theirs})
    assert [r.reason for r in refusals] == [Reason.WRITE_CONFLICT]


def test_directory_prefix_counts_as_overlap(tmp_path):
    mine = parse_task(write(tmp_path, "a.md", make_task(task_id="T002", write_set=("src/",))))
    theirs = parse_task(write(tmp_path, "b.md", make_task(task_id="T005", write_set=("src/core.py",))))
    assert write_conflicts(mine, {"T005": theirs})


def test_disjoint_write_sets_do_not_conflict(tmp_path):
    mine = parse_task(write(tmp_path, "a.md", make_task(task_id="T002", write_set=("pyproject.toml",))))
    theirs = parse_task(write(tmp_path, "b.md", make_task(task_id="T005", write_set=("src/core.py",))))
    assert write_conflicts(mine, {"T005": theirs}) == []


def test_thief_repo_is_evaluated_independently(tmp_path, repos):
    """Same task ID, different repository — readiness is computed per repository."""
    police = _evaluate(tmp_path, repos["police"], make_task(gates=(CRITERION_GATE,)))
    thief = _evaluate(tmp_path, repos["thief"], make_task(gates=(CRITERION_GATE,)))
    assert police.ready and thief.ready


def test_registers_are_read_from_the_repo_under_evaluation(tmp_path):
    """A repo whose register RESOLVES PLANQ-002 makes the same task ready."""
    resolved = build_repo(tmp_path / "alt", "resolved")
    register = resolved / "docs" / "spec" / "OPEN_QUESTIONS.md"
    register.write_text(
        register.read_text().replace("| `TBD_TEAM_DECISION` | project team | T002 |", "| `chosen` | project team | T002 |")
    )
    verdict = _evaluate(tmp_path, resolved, make_task(gates=(START_GATE,)))
    assert verdict.ready is True


def test_load_tasks_and_evaluate_end_to_end(repos):
    registers = Registers(repos["police"], ProjectConfig())
    tasks = load_tasks(repos["police"], ("docs/tasks",))
    verdict = evaluate(tasks["T002"], tasks, registers)
    assert verdict.ready is True
