"""The dry-run path, end to end, against throwaway repositories.

A dry run is the safety valve: it must resolve routing, budgets, context and
validation for a real task and still touch nothing. These tests assert the
"touch nothing" half as strictly as the "resolve everything" half.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from orchestrator import runner
from orchestrator.config import CONFIG_DIR
from orchestrator.errors import Reason
from orchestrator.workflow import RunSpec, ValidationLevel
from orchestrator.worktrees import WorktreeManager

SHIPPED = yaml.safe_load((CONFIG_DIR / "models.example.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def shipped_config(config, monkeypatch):
    """The fixture repos, but the REAL shipped routing table."""
    monkeypatch.setattr(
        "orchestrator.model_router._probe_http", lambda url, timeout=2.0: (True, "HTTP 200")
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    config.models.clear()
    config.models.update(copy.deepcopy(SHIPPED))  # the fixture hands us our own copy
    monkeypatch.setattr(
        "orchestrator.model_router.ModelRouter.catalog",
        lambda self: {spec["id"]: {} for spec in SHIPPED["models"].values() if spec.get("id")},
    )
    return config


@pytest.fixture
def worktree(shipped_config):
    return WorktreeManager(shipped_config).create(shipped_config.repo("police"), "T002", None)


def test_a_dry_run_resolves_routing_budgets_and_context(shipped_config, worktree):
    plan = runner.plan(shipped_config, RunSpec(repo="police", task="T002"))

    assert plan.executable is True
    assert plan.routing["implement"]["model"] == "gemini-3.7-flash"
    assert plan.routing["review"]["model"] == "deepseek-v4-pro"
    assert plan.routing["fix"]["paid"] is False
    assert plan.budgets["total"]["hard_usd"] == 1.00
    assert plan.context["files"] > 0
    assert plan.context["estimated_tokens"] > 0
    assert plan.write_set


def test_a_dry_run_shows_the_validation_plan_and_the_human_gates(shipped_config, worktree):
    spec = RunSpec(repo="police", task="T002", level3=("uv run pytest",))

    plan = runner.plan(shipped_config, spec)

    assert plan.validation_plan[str(ValidationLevel.LEVEL3)] == ["uv run pytest"]
    # Level 1 falls back to the task's own declared verification commands.
    assert plan.validation_plan[str(ValidationLevel.LEVEL1)]
    assert any("merge" in gate for gate in plan.human_gates)


def test_a_dry_run_dispatches_no_worker_and_writes_no_ledger_row(
    shipped_config, worktree, monkeypatch
):
    """The point of a dry run: no worker is spawned and nothing is billed.

    Patching the WORKER transport specifically, not `subprocess` wholesale —
    a dry run legitimately shells out to git to resolve the base commit.
    """
    def explode(*args, **kwargs):
        raise AssertionError("a dry run spawned a Pi worker")

    monkeypatch.setattr("orchestrator.pi_rpc.SubprocessTransport.start", explode)

    runner.plan(shipped_config, RunSpec(repo="police", task="T002"))

    from orchestrator.budget import Ledger

    assert Ledger(shipped_config).entries() == []


def test_a_dry_run_creates_no_run_directory(shipped_config, worktree):
    runner.plan(shipped_config, RunSpec(repo="police", task="T002"))

    assert not (shipped_config.state_dir / "runs").exists()


def test_a_dry_run_leaves_the_worktree_untouched(shipped_config, worktree):
    from orchestrator import gitio

    runner.plan(shipped_config, RunSpec(repo="police", task="T002"))

    from pathlib import Path

    assert gitio.changed_paths(Path(worktree.path), worktree.base_sha) == []


def test_a_dry_run_reports_a_refusal_instead_of_raising_when_a_stage_is_unroutable(
    shipped_config, worktree
):
    del shipped_config.models["escalation"]["medium"]["review"]

    plan = runner.plan(shipped_config, RunSpec(repo="police", task="T002"))

    assert plan.executable is False
    assert plan.refusals[0].reason == Reason.CONFIG_INVALID


def test_a_dry_run_renders_a_human_report(shipped_config, worktree):
    rendered = runner.plan(shipped_config, RunSpec(repo="police", task="T002")).render()

    assert "No paid model was called" in rendered
    assert "ROUTING" in rendered and "BUDGETS" in rendered
    assert "VALIDATION PLAN" in rendered and "HUMAN GATES" in rendered
