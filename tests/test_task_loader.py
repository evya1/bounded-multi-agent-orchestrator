"""YAML frontmatter parsing, with nested gates as the headline case."""

from __future__ import annotations

import pytest

from orchestrator.errors import OrchestratorError, Reason
from orchestrator.task_loader import load_tasks, parse_task
from tests.conftest import make_task, write


def test_nested_gates_survive_as_structured_data(tmp_path):
    """v3's line parser flattened this; a gate without `blocks` cannot be enforced."""
    path = write(
        tmp_path,
        "T002.md",
        make_task(
            gates=(
                {"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "criterion"},
                {"id": "G-OFFICIAL", "kind": "input_gate", "scope": "official_schemas", "blocks": "start"},
            )
        ),
    )
    task = parse_task(path)

    assert len(task.gates) == 2
    first, second = task.gates
    assert (first.id, first.kind, first.scope, first.blocks) == (
        "PLANQ-002",
        "decision",
        "dependency_lock",
        "criterion",
    )
    assert first.blocks_start is False
    assert second.blocks_start is True
    # the whole point: gates are mappings, not strings
    assert all(isinstance(gate.as_dict(), dict) for gate in task.gates)


def test_list_fields_are_lists_not_strings(tmp_path):
    path = write(tmp_path, "T002.md", make_task(write_set=("pyproject.toml", "uv.lock")))
    task = parse_task(path)
    assert task.implements == ["NET-001", "QR-014"]
    assert task.write_set == ["pyproject.toml", "uv.lock"]
    assert task.read_set == ["config/repo_quality.toml"]
    assert task.depends_on == []


def test_empty_frontmatter_fields_do_not_crash(tmp_path):
    path = write(tmp_path, "T003.md", make_task(task_id="T003", implements=(), gates=(), write_set=()))
    task = parse_task(path)
    assert task.implements == []
    assert task.gates == []
    assert task.write_set == []


def test_missing_frontmatter_is_an_error(tmp_path):
    path = write(tmp_path, "bad.md", "# no frontmatter here\n")
    with pytest.raises(OrchestratorError) as excinfo:
        parse_task(path)
    assert excinfo.value.refusal.reason is Reason.CONFIG_INVALID


def test_flattened_gates_are_rejected(tmp_path):
    """A gate list of plain strings must fail loudly, not parse into nonsense."""
    path = write(
        tmp_path,
        "T004.md",
        "---\nid: T004\nstatus: ready\ngates:\n  - PLANQ-002\nrisk: low\n---\n\n# body\n",
    )
    task = parse_task(path)
    with pytest.raises(OrchestratorError) as excinfo:
        _ = task.gates
    assert "flattened" in str(excinfo.value)


def test_verification_commands_come_from_the_verification_section(tmp_path):
    path = write(tmp_path, "T002.md", make_task(verification="uv run pytest"))
    assert parse_task(path).verification_commands == ["uv run pytest"]


def test_load_tasks_indexes_by_id(repos):
    tasks = load_tasks(repos["police"], ("docs/tasks",))
    assert set(tasks) == {"T002"}
    assert tasks["T002"].id == "T002"
