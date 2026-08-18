"""Workflow-level guarantees: privileges, budgets, write sets, and dry runs.

Every test here runs the real state machine against a scripted dispatcher, so
the decisions about whether to spend money are exercised without spending any.
"""

from __future__ import annotations

import copy
from typing import ClassVar

import pytest
import yaml

from orchestrator import budget as budget_mod
from orchestrator.config import CONFIG_DIR, BudgetConfig, Config, ProjectConfig
from orchestrator.errors import Reason
from orchestrator.failures import Action, FailureClass, classify_outcome, policy_for
from orchestrator.model_router import ModelRouter
from orchestrator.pi_rpc import Lifecycle, StopReason, Telemetry, WorkerOutcome
from orchestrator.workflow import Dispatch, Phase, RunSpec, ValidationLevel, WorkflowEngine

SHIPPED = yaml.safe_load((CONFIG_DIR / "models.example.yaml").read_text(encoding="utf-8"))


class _Task:
    """A minimal stand-in for a loaded Task, with only the fields under test."""

    id = "T002"
    component = "reporting"
    task_type = "feature"
    risk = "medium"
    implements: ClassVar[list[str]] = ["REQ-1"]
    write_set: ClassVar[list[str]] = ["src/reporting/"]
    verification_commands: ClassVar[list[str]] = ["uv run pytest -q"]


@pytest.fixture
def config(tmp_path):
    return Config(
        workspace=tmp_path,
        repos={},
        project=ProjectConfig(),
        governance_paths=(),
        review_policy={"medium": ("diff",)},
        resources={},
        budget=BudgetConfig(),
        models=copy.deepcopy(SHIPPED),
    )


@pytest.fixture
def router(config, monkeypatch):
    monkeypatch.setattr("orchestrator.model_router._probe_http", lambda url, timeout=2.0: (True, "HTTP 200"))
    router = ModelRouter(config.models, {"OPENROUTER_API_KEY": "x"})
    router._catalog = {spec["id"]: {} for spec in config.models["models"].values() if spec.get("id")}
    return router


def _settled(cost: float = 0.0, text: str = "ok") -> WorkerOutcome:
    return WorkerOutcome(
        worker_id="w", role="writer", provider="openrouter", model="m",
        lifecycle=str(Lifecycle.COMPLETE), stop_reason=str(StopReason.SETTLED), settled=True,
        final_text=text,
        telemetry=Telemetry(cost_usd=cost, cost_source="provider_reported"),
    )


def _engine(config, router, dispatcher, tmp_path, total_hard=1.00):
    ledger = budget_mod.Ledger(config)
    run_budget = budget_mod.RunBudget(
        ledger, "run-1", budget_mod.Envelope("run", total_hard * 0.6, total_hard)
    )
    for role, envelope in budget_mod.envelopes_from_roles(router.roles).items():
        run_budget.set_role(role, envelope)
    return WorkflowEngine(
        repo_name="police",
        task=_Task(),
        task_run_id="run-1",
        router=router,
        run_budget=run_budget,
        dispatcher=dispatcher,
        validator=lambda level: (True, "ok"),
        auditor=lambda: (True, "clean"),
        complexity="medium",
    )


# --------------------------------------------------------------- privileges


def test_the_reviewer_is_dispatched_with_no_tools(config, router, tmp_path):
    seen: list[Dispatch] = []
    engine = _engine(config, router, lambda request: (seen.append(request), _settled())[1], tmp_path)

    engine._run_worker(Phase.REVIEW, "review", "packet")

    assert seen[0].read_only is True
    assert seen[0].choice.privileges["tools"] == "none"
    assert seen[0].limits.max_tool_calls == 0


def test_the_reviewer_answers_once(config, router):
    assert router.limits_for("reviewer").max_model_calls == 2  # one answer plus one repair
    assert router.limits_for("resolver").max_model_calls == 1


def test_the_implementer_is_dispatched_write_set_only(config, router, tmp_path):
    seen: list[Dispatch] = []
    engine = _engine(config, router, lambda request: (seen.append(request), _settled())[1], tmp_path)

    engine._run_worker(Phase.WRITER, "implement", "do it")

    assert seen[0].read_only is False
    assert seen[0].choice.privileges["write_set_only"] is True
    assert seen[0].choice.privileges["bash"] is False


# ------------------------------------------------------------------ budgets


def test_a_generation_that_would_cross_the_run_hard_budget_is_never_dispatched(
    config, router, tmp_path
):
    """CASE I — the check is forward-looking and happens BEFORE the call."""
    dispatched: list[Dispatch] = []
    engine = _engine(
        config, router, lambda request: (dispatched.append(request), _settled())[1], tmp_path,
        total_hard=0.0001,
    )

    stage, outcome = engine._run_worker(Phase.WRITER, "implement", "x" * 400_000)

    assert dispatched == [], "a paid worker was dispatched despite the hard budget"
    assert outcome is None
    assert stage.failure == str(FailureClass.BUDGET_EXCEEDED)
    assert stage.action == str(Action.HUMAN_APPROVAL)
    assert engine.report.refusals[0].reason == Reason.BUDGET_REFUSED


def test_a_role_hard_budget_is_enforced_independently_of_the_run_budget(config, router, tmp_path):
    dispatched: list[Dispatch] = []
    engine = _engine(
        config, router, lambda request: (dispatched.append(request), _settled())[1], tmp_path
    )
    engine.run_budget.set_role("reviewer", budget_mod.Envelope("reviewer", 0.001, 0.002))

    engine._run_worker(Phase.REVIEW, "review", "x" * 400_000)

    assert dispatched == []
    assert "role:reviewer" in engine.report.refusals[0].message


def test_a_local_worker_is_dispatched_without_a_budget_check(config, router, tmp_path):
    dispatched: list[Dispatch] = []
    engine = _engine(
        config, router, lambda request: (dispatched.append(request), _settled())[1], tmp_path,
        total_hard=0.0001,
    )

    stage, _outcome = engine._run_worker(Phase.FIX, "fix", "x" * 400_000)

    assert len(dispatched) == 1, "a zero-cost local worker was blocked by a dollar budget"
    assert dispatched[0].choice.paid is False
    assert stage.passed is True


def test_a_soft_budget_warns_and_proceeds(config, router, tmp_path):
    engine = _engine(config, router, lambda request: _settled(), tmp_path)
    engine.run_budget.set_role("reviewer", budget_mod.Envelope("reviewer", 0.0001, 10.0))

    verdict = engine.run_budget.authorize_generation("reviewer", 0.05)

    assert verdict.allowed is True
    assert any("SOFT budget" in warning for warning in verdict.warnings)


def test_budget_scopes_are_reported_for_inspection(config, router, tmp_path):
    engine = _engine(config, router, lambda request: _settled(), tmp_path)

    verdict = engine.run_budget.authorize_generation("reviewer", 0.01)

    scopes = {row["scope"] for row in verdict.checked}
    assert scopes == {"role:reviewer", "run"}


# ------------------------------------------------------------- fail closed


def test_preflight_refuses_a_configuration_that_can_route_to_claude(config, router, tmp_path):
    """CASE J — a Claude route stops the run at preflight, before anything spawns."""
    router.config["models"]["c"] = {
        "provider": "openrouter", "id": "anthropic/claude-sonnet-5", "family": "anthropic"
    }
    router.roles["reviewer"]["primary"] = "c"
    engine = _engine(config, router, lambda request: _settled(), tmp_path)

    refusals = engine.preflight()

    assert refusals
    assert any("no Claude runtime route" in refusal.message for refusal in refusals)


def test_preflight_refuses_an_unconfigured_stage(config, router, tmp_path):
    del router.escalation["medium"]["review"]
    engine = _engine(config, router, lambda request: _settled(), tmp_path)

    refusals = engine.preflight()

    assert any(refusal.reason == Reason.CONFIG_INVALID for refusal in refusals)
    assert any("fails closed" in refusal.message for refusal in refusals)


def test_preflight_passes_on_the_shipped_configuration(config, router, tmp_path):
    assert _engine(config, router, lambda request: _settled(), tmp_path).preflight() == []


def test_an_unavailable_model_blocks_the_stage_without_substituting(config, router, tmp_path):
    dispatched: list[Dispatch] = []
    router.models["deepseek-v4-pro"]["id"] = "deepseek/gone"
    engine = _engine(
        config, router, lambda request: (dispatched.append(request), _settled())[1], tmp_path
    )

    stage, outcome = engine._run_worker(Phase.REVIEW, "review", "packet")

    assert dispatched == []
    assert outcome is None
    assert stage.failure == str(FailureClass.MODEL_UNAVAILABLE)
    assert policy_for(FailureClass.MODEL_UNAVAILABLE).action == Action.FAIL_CLOSED


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("lifecycle", "stop", "expected"),
    [
        (Lifecycle.COMPLETE, StopReason.SETTLED, FailureClass.NONE),
        (Lifecycle.BUDGET_EXCEEDED, StopReason.HARD_BUDGET, FailureClass.BUDGET_EXCEEDED),
        (Lifecycle.TIMED_OUT, StopReason.INACTIVITY, FailureClass.TIMEOUT),
        (Lifecycle.TIMED_OUT, StopReason.WALL_DEADLINE, FailureClass.TIMEOUT),
        (Lifecycle.ABORTED, StopReason.MAX_MODEL_CALLS, FailureClass.TIMEOUT),
        (Lifecycle.PROCESS_LOST, StopReason.PROCESS_EXITED, FailureClass.PROCESS_LOST),
    ],
)
def test_outcomes_are_classified_from_lifecycle_not_prose(lifecycle, stop, expected):
    outcome = WorkerOutcome(
        worker_id="w", role="r", provider="p", model="m",
        lifecycle=str(lifecycle), stop_reason=str(stop), settled=lifecycle == Lifecycle.COMPLETE,
        final_text="the model wrote the word FAILED in its prose",
    )

    assert classify_outcome(outcome) == expected


def test_a_transient_provider_error_retries_the_same_model_exactly_once():
    outcome = WorkerOutcome(
        worker_id="w", role="r", provider="p", model="m",
        lifecycle=str(Lifecycle.PROCESS_LOST), stop_reason=str(StopReason.PROCESS_EXITED),
        settled=False, detail="upstream returned 503 temporarily unavailable",
    )

    failure = classify_outcome(outcome)
    policy = policy_for(failure)

    assert failure == FailureClass.TRANSIENT_PROVIDER
    assert policy.action == Action.RETRY_SAME_MODEL
    assert policy.max_attempts == 1
    assert policy.substitute_model is False


def test_a_local_server_refusing_connections_never_becomes_a_paid_call():
    outcome = WorkerOutcome(
        worker_id="w", role="r", provider="local_llama", model="qwen3-coder-30b",
        lifecycle=str(Lifecycle.FAILED), stop_reason=str(StopReason.SPAWN_FAILED),
        settled=False, detail="spawn failed: ECONNREFUSED 127.0.0.1:8090",
    )

    policy = policy_for(classify_outcome(outcome))

    assert policy.action == Action.FAIL_CLOSED
    assert "do not silently move the work" in policy.explanation


def test_no_policy_in_the_taxonomy_substitutes_a_model():
    from orchestrator.failures import POLICIES

    assert all(policy.substitute_model is False for policy in POLICIES.values())


def test_every_taxonomy_entry_is_bounded():
    from orchestrator.failures import POLICIES

    assert all(policy.max_attempts <= 1 for policy in POLICIES.values())


# ---------------------------------------------------------------- run specs


def test_a_run_config_may_not_name_a_model(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text("repo: police\ntask: T002\nmodel: anthropic/claude-sonnet-5\n")

    from orchestrator.errors import OrchestratorError

    with pytest.raises(OrchestratorError) as caught:
        RunSpec.load(path)

    assert caught.value.refusal.reason == Reason.CONFIG_INVALID
    assert "may not name a model" in caught.value.refusal.message


def test_the_shipped_example_run_config_loads():
    spec = RunSpec.load(CONFIG_DIR / "runs" / "example-task.yaml")

    assert spec.repo and spec.task
    assert spec.commands_for(ValidationLevel.LEVEL3)


def test_level_1_falls_back_to_the_tasks_own_verification_commands():
    spec = RunSpec(repo="police", task="T002")

    assert spec.commands_for(ValidationLevel.LEVEL1, ("uv run pytest -q",)) == ("uv run pytest -q",)
