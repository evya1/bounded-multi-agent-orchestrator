"""Bounded context: what goes in, what stays out, and what fails loudly."""

from __future__ import annotations

import pytest

from orchestrator.context_compiler import ContextCompiler, Kind, render_human
from orchestrator.errors import OrchestratorError, Reason
from orchestrator.registers import Registers
from orchestrator.task_loader import load_tasks, parse_task
from tests.conftest import make_task, write


def _compile(config, repo_name, task=None, strict=True):
    repo = config.repo(repo_name)
    registers = Registers(repo.path, config.project)
    tasks = load_tasks(repo.path, config.project.task_dirs)
    compiler = ContextCompiler(config, repo, registers)
    return compiler.compile(task or tasks["T002"], "abc123", strict=strict)


def test_only_declared_material_is_included(config):
    manifest = _compile(config, "police")
    refs = set(manifest.included_refs)

    # present: AGENTS.md, the task, context_files, read_set, named IDs, write_set
    assert "AGENTS.md" in refs
    assert "docs/tasks/T002-fixture.md" in refs
    assert "docs/PRD.md" in refs
    assert "config/repo_quality.toml" in refs
    assert {"NET-001", "QR-014", "PLANQ-002"} <= refs
    assert "pyproject.toml" in refs


def test_unrelated_requirements_are_not_loaded(config):
    manifest = _compile(config, "police")
    refs = set(manifest.included_refs)
    assert "GAME-001" not in refs
    assert "STRAT-003" not in refs
    # and the whole register is never a FULL_FILE
    assert "docs/spec/CANONICAL_REQUIREMENTS.md" not in refs


def test_requirements_are_targeted_excerpts_not_whole_files(config):
    manifest = _compile(config, "police")
    net = next(item for item in manifest.items if item.ref == "NET-001")
    assert net.kind is Kind.TARGETED
    assert net.source == "docs/spec/CANONICAL_REQUIREMENTS.md"
    assert net.line > 0
    assert "FastMCP" in net.text


def test_ids_mentioned_only_in_prose_are_excluded_not_loaded(config):
    """The task body says PLANQ-007 owns the GUI. That is an exclusion, not a request."""
    manifest = _compile(config, "police")
    assert "PLANQ-007" not in set(manifest.included_refs)
    excluded = {item.ref: item for item in manifest.items if item.kind is Kind.EXCLUDED}
    assert "PLANQ-007" in excluded
    assert "not declared in frontmatter" in excluded["PLANQ-007"].reason


def test_adr_referenced_by_path_in_the_body_is_included(config):
    manifest = _compile(config, "police")
    assert "docs/decisions/ADR-002-ci-uv-bootstrap.md" in set(manifest.included_refs)
    # an ADR that is NOT referenced stays out
    assert "docs/decisions/ADR-009-unrelated.md" not in set(manifest.included_refs)


def test_exclusions_are_stated_explicitly(config):
    manifest = _compile(config, "police")
    reasons = " ".join(
        item.reason for item in manifest.items if item.kind is Kind.EXCLUDED
    )
    refs = " ".join(item.ref for item in manifest.items if item.kind is Kind.EXCLUDED)
    assert "canonical requirement" in reasons
    assert "bundle-wide planning masters" in reasons
    assert "docs/components/" in refs


def test_write_set_contents_are_included_and_gaps_marked_missing(config):
    manifest = _compile(config, "police")
    by_ref = {item.ref: item for item in manifest.items}
    assert by_ref["pyproject.toml"].kind is Kind.WRITE_OWNED
    assert by_ref["pyproject.toml"].text is not None
    # uv.lock does not exist yet: MISSING but not an error, the task creates it
    assert by_ref["uv.lock"].kind is Kind.MISSING
    assert "this task creates it" in by_ref["uv.lock"].reason


def test_missing_declared_context_file_is_an_error_not_a_search(config, tmp_path):
    body = make_task().replace("  - docs/PRD.md", "  - docs/DOES-NOT-EXIST.md")
    task = parse_task(write(tmp_path, "T002.md", body))
    with pytest.raises(OrchestratorError) as excinfo:
        _compile(config, "police", task)
    assert excinfo.value.refusal.reason is Reason.CONTEXT_COMPILE_FAILED
    assert "docs/DOES-NOT-EXIST.md" in str(excinfo.value)


def test_missing_read_set_path_is_an_error(config, tmp_path):
    body = make_task().replace("  - config/repo_quality.toml", "  - config/nope.toml")
    task = parse_task(write(tmp_path, "T002.md", body))
    with pytest.raises(OrchestratorError):
        _compile(config, "police", task)


def test_unresolved_requirement_id_is_an_error(config, tmp_path):
    body = make_task(implements=("NET-001", "NOPE-999"))
    task = parse_task(write(tmp_path, "T002.md", body))
    with pytest.raises(OrchestratorError) as excinfo:
        _compile(config, "police", task)
    assert "NOPE-999" in str(excinfo.value)


def test_allow_missing_reports_instead_of_raising(config, tmp_path):
    body = make_task().replace("  - docs/PRD.md", "  - docs/DOES-NOT-EXIST.md")
    task = parse_task(write(tmp_path, "T002.md", body))
    manifest = _compile(config, "police", task, strict=False)
    assert [r.reason for r in manifest.refusals] == [Reason.CONTEXT_FILE_MISSING]


def test_manifests_are_repository_scoped(config):
    police = _compile(config, "police")
    thief = _compile(config, "thief")
    assert police.repo_identity != thief.repo_identity
    police_agents = next(i for i in police.items if i.ref == "AGENTS.md")
    thief_agents = next(i for i in thief.items if i.ref == "AGENTS.md")
    assert "police peer" in police_agents.text
    assert "thief peer" in thief_agents.text


def test_rendered_prompt_contains_only_included_items(config):
    manifest = _compile(config, "police")
    rendered = manifest.render_prompt_context()
    assert "FastMCP" in rendered           # NET-001 excerpt
    assert "The board MUST be square" not in rendered   # GAME-001 never loaded


def test_human_render_groups_every_category(config):
    text = render_human(_compile(config, "police"))
    for heading in ("FULL FILES", "TARGETED IDS / EXCERPTS", "WRITE-OWNED", "MISSING", "EXCLUDED"):
        assert heading in text
