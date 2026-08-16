"""Model routing, budget refusal, resource serialization, state machine, prompts."""

from __future__ import annotations

import pytest

from orchestrator import prompts, resources, write_guard
from orchestrator.adapters import FakeRunner, PiAdapter, RunResult, parse_pi_json
from orchestrator.budget import CostSource, Ledger, LedgerEntry, estimate_cost
from orchestrator.errors import OrchestratorError, Reason
from orchestrator.model_router import ModelRouter
from orchestrator.state import State, StateStore
from orchestrator.task_loader import load_tasks

ENV_PAID = {"FIXTURE_KEY": "sentinel-not-a-real-key"}


# ------------------------------------------------------------------- routing


def test_routing_is_configuration_not_code(config):
    router = ModelRouter(config.models, ENV_PAID)
    assert router.role_for("implement", "medium") == "local_executor"
    assert router.role_for("review", "medium") == "code_reviewer"
    assert router.role_for("review", "low") == "value_reasoner"


def test_local_model_resolves_when_reachable(config, monkeypatch):
    monkeypatch.setattr("orchestrator.model_router._probe_http", lambda url, timeout=2.0: (True, "HTTP 200"))
    router = ModelRouter(config.models, {**ENV_PAID, "FIXTURE_LOCAL_URL": "http://127.0.0.1:9"})
    choice, refusals, _ = router.resolve("implement", "medium")
    assert refusals == []
    assert choice.name == "local-qwen"
    assert choice.paid is False


def test_unreachable_local_model_refuses(config, monkeypatch):
    monkeypatch.setattr("orchestrator.model_router._probe_http", lambda url, timeout=2.0: (False, "unreachable"))
    router = ModelRouter(config.models, {**ENV_PAID, "FIXTURE_LOCAL_URL": "http://127.0.0.1:9"})
    choice, refusals, _ = router.resolve("implement", "medium")
    assert choice is None
    assert refusals[0].reason is Reason.MODEL_UNAVAILABLE


def test_missing_credentials_make_a_paid_model_unavailable(config):
    router = ModelRouter(config.models, {})
    assert router.availability("cheap-cloud").available is False
    assert "credentials not configured" in router.availability("cheap-cloud").detail


def test_key_presence_is_reported_without_reading_the_value(config):
    router = ModelRouter(config.models, ENV_PAID)
    assert router.key_available("cloud") is True
    assert router.key_available("local") is None


def test_frontier_reviewer_is_never_silently_downgraded(config):
    """allow_weaker_fallback: false turns an unavailable primary into a refusal."""
    router = ModelRouter(config.models, ENV_PAID)
    router.escalation["high"]["review"] = "strict_absent"
    router._catalog = {"vendor/cheap": {}, "vendor/frontier": {}}  # "vendor/absent" is not listed
    choice, refusals, _ = router.resolve("review", "high")
    assert choice is None
    assert refusals[0].reason is Reason.MODEL_UNAVAILABLE
    assert "refusing to substitute a weaker model" in refusals[0].message


def test_permitted_fallback_is_reported(config):
    router = ModelRouter(config.models, ENV_PAID)
    router.models["cheap-cloud"]["id"] = "vendor/absent-from-catalog"
    router.roles["value_reasoner"]["allow_weaker_fallback"] = True
    router._catalog = {"vendor/frontier": {}}
    choice, refusals, notes = router.resolve("review", "low")
    assert refusals == []
    assert choice.name == "frontier"
    assert choice.substituted_for == "cheap-cloud"
    assert any("SUBSTITUTED" in note for note in notes)


def test_manual_override(config):
    router = ModelRouter(config.models, ENV_PAID)
    choice, refusals, notes = router.resolve("review", "medium", override="cheap-cloud")
    assert refusals == [] and choice.name == "cheap-cloud"
    assert notes == ["manual override: cheap-cloud"]


def test_unknown_override_is_refused(config):
    router = ModelRouter(config.models, ENV_PAID)
    choice, refusals, _ = router.resolve("review", "medium", override="nonexistent")
    assert choice is None and refusals[0].reason is Reason.MODEL_UNAVAILABLE


def test_diversity_loss_is_reported_for_high_risk(config):
    router = ModelRouter(config.models, ENV_PAID)
    same = {stage: router._choice("r", "frontier", stage, None) for stage in ("plan", "implement", "review")}
    refusal = router.diversity_check("high", same)
    assert refusal is not None and refusal.reason is Reason.DIVERSITY_LOST

    mixed = dict(same)
    mixed["implement"] = router._choice("r", "local-qwen", "implement", None)
    assert router.diversity_check("high", mixed) is None


def test_diversity_is_not_enforced_below_the_configured_threshold(config):
    router = ModelRouter(config.models, ENV_PAID)
    same = {stage: router._choice("r", "frontier", stage, None) for stage in ("plan", "implement", "review")}
    assert router.diversity_check("low", same) is None


def test_privileges_come_from_configuration(config):
    router = ModelRouter(config.models, ENV_PAID)
    plan = router._choice("r", "frontier", "plan", None)
    implement = router._choice("r", "local-qwen", "implement", None)
    assert plan.privileges["tools"] == "none"
    assert implement.privileges["write_set_only"] is True


def test_planner_argv_disables_every_tool(config):
    """Enforcement is argv-level, not prose-level."""
    router = ModelRouter(config.models, ENV_PAID)
    argv = PiAdapter({"executable": "pi"}).build_argv(
        router._choice("r", "frontier", "plan", None), __import__("pathlib").Path("/tmp/p.md"), "plan"
    )
    assert "--no-tools" in argv
    assert "--tools" not in argv


def test_implementer_argv_allows_edit_but_not_bash(config, tmp_path):
    router = ModelRouter(config.models, ENV_PAID)
    policy = write_guard.policy_for(tmp_path, ["pyproject.toml"])
    argv = PiAdapter({"executable": "pi"}).build_argv(
        router._choice("r", "local-qwen", "implement", None),
        __import__("pathlib").Path("/tmp/p.md"),
        "implement",
        policy,
    )
    assert "--tools" in argv
    tools = argv[argv.index("--tools") + 1]
    assert "edit" in tools and "bash" not in tools


# -------------------------------------------------------------------- budget


def test_budget_refuses_a_call_over_the_per_call_cap(config):
    ledger = Ledger(config)
    refusal = ledger.authorize(paid=True, estimated_usd=5.0)
    assert refusal.reason is Reason.BUDGET_REFUSED
    assert "max_usd_per_call" in refusal.message


def test_budget_refuses_when_the_reserve_would_be_breached(config):
    ledger = Ledger(config)
    for _ in range(7):
        ledger.append(_entry(cost=0.45))
    refusal = ledger.authorize(paid=True, estimated_usd=0.40)
    assert refusal.reason is Reason.BUDGET_REFUSED
    assert "reserve" in refusal.message


def test_budget_allows_a_call_within_limits(config):
    assert Ledger(config).authorize(paid=True, estimated_usd=0.10) is None


def test_local_calls_never_consume_budget(config):
    ledger = Ledger(config)
    ledger.append(_entry(cost=0.0, paid=False))
    assert ledger.authorize(paid=False, estimated_usd=99.0) is None
    assert ledger.status().spent_usd == 0.0


def test_ledger_is_append_only_and_attributes_spend(config):
    ledger = Ledger(config)
    ledger.append(_entry(cost=0.10, model="vendor/cheap", role="value_reasoner"))
    ledger.append(_entry(cost=0.25, model="vendor/frontier", role="code_reviewer"))
    status = ledger.status()
    assert status.spent_usd == pytest.approx(0.35)
    assert status.by_model["vendor/frontier"] == pytest.approx(0.25)
    assert status.by_role["value_reasoner"] == pytest.approx(0.10)
    assert status.by_task["police/T002"] == pytest.approx(0.35)
    assert len(ledger.path.read_text().strip().splitlines()) == 2


def test_cost_provenance_is_tracked_separately(config):
    ledger = Ledger(config)
    ledger.append(_entry(cost=0.10, source=CostSource.REPORTED))
    ledger.append(_entry(cost=0.20, source=CostSource.ESTIMATED))
    status = ledger.status()
    assert status.reported_usd == pytest.approx(0.10)
    assert status.estimated_usd == pytest.approx(0.20)


def test_cost_estimation_arithmetic():
    assert estimate_cost({"input": 2.0, "output": 10.0}, 1_000_000, 100_000) == pytest.approx(3.0)


def test_ledger_never_records_prompt_text_or_secrets(config):
    ledger = Ledger(config)
    ledger.append(_entry(cost=0.01))
    fields = set(LedgerEntry.__dataclass_fields__)
    assert "prompt" not in fields and "api_key" not in fields
    assert "sentinel" not in ledger.path.read_text()


def _entry(cost=0.0, paid=True, model="vendor/cheap", role="value_reasoner",
           source=CostSource.ESTIMATED) -> LedgerEntry:
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    return LedgerEntry(
        at=now.isoformat(), day=now.strftime("%Y-%m-%d"), repo="police", task_id="T002",
        stage="review", role=role, provider="cloud", model=model, paid=paid,
        input_tokens=100, output_tokens=50, cost_usd=cost, cost_source=str(source),
    )


# ------------------------------------------------ repository-global resources


def test_dependency_files_acquire_the_repository_resource(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    wanted = resources.required_resources("police", tasks["T002"], config.resources)
    assert {str(h) for h in wanted} == {"police:dependency_manifest", "police:dependency_lock"}


def test_police_and_thief_resources_are_independent(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    police = resources.required_resources("police", tasks["T002"], config.resources)
    thief = resources.required_resources("thief", tasks["T002"], config.resources)
    assert set(police).isdisjoint(thief)
    # both may be held at once: neither conflicts with the other's holders
    holders = {str(h): ["T002"] for h in thief}
    assert resources.conflicts(police, holders) == []


def test_a_held_resource_conflicts_within_one_repository(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    wanted = resources.required_resources("police", tasks["T002"], config.resources)
    conflicts = resources.conflicts(wanted, {"police:dependency_lock": ["T030"]})
    assert [str(h) for h, _ in conflicts] == ["police:dependency_lock"]


def test_state_store_reports_resource_holders(config):
    store = StateStore(config)
    repo = config.repo("police")
    state = store.get_or_create(repo, "T002")
    state.resources = ["police:dependency_lock"]
    store.save(repo, state)
    store.transition(repo, state, State.PLANNED)
    assert store.resource_holders(repo) == {"police:dependency_lock": ["T002"]}


# ------------------------------------------------------------- state machine


def test_legal_transition_sequence(config):
    store = StateStore(config)
    repo = config.repo("police")
    state = store.get_or_create(repo, "T002")
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED, State.REVIEWED,
                   State.DIFF_APPROVAL_REQUIRED, State.PR_READY):
        store.transition(repo, state, target)
    assert state.current is State.PR_READY
    assert len(state.history) == 6


def test_verification_cannot_be_skipped(config):
    store = StateStore(config)
    repo = config.repo("police")
    state = store.get_or_create(repo, "T002")
    store.transition(repo, state, State.PLANNED)
    store.transition(repo, state, State.IMPLEMENTED)
    with pytest.raises(OrchestratorError) as excinfo:
        store.transition(repo, state, State.PR_READY)
    assert excinfo.value.refusal.reason is Reason.ILLEGAL_TRANSITION


def test_rejection_returns_to_an_earlier_stage(config):
    store = StateStore(config)
    repo = config.repo("police")
    state = store.get_or_create(repo, "T002")
    for target in (State.PLANNED, State.IMPLEMENTED, State.VERIFIED):
        store.transition(repo, state, target)
    store.transition(repo, state, State.IMPLEMENTED, "review rejected")
    assert state.current is State.IMPLEMENTED


def test_states_are_namespaced_per_repository(config):
    store = StateStore(config)
    police_state = store.get_or_create(config.repo("police"), "T002")
    store.transition(config.repo("police"), police_state, State.PLANNED)
    assert store.load(config.repo("thief"), "T002") is None


# ------------------------------------------------------- prompts / provenance


def test_every_prompt_carries_the_stop_contract(config):
    from orchestrator.context_compiler import ContextCompiler
    from orchestrator.registers import Registers

    repo = config.repo("police")
    tasks = load_tasks(repo.path, config.project.task_dirs)
    manifest = ContextCompiler(config, repo, Registers(repo.path, config.project)).compile(
        tasks["T002"], "base1"
    )
    for stage in ("plan", "implement", "review"):
        text = prompts.build_prompt(stage, tasks["T002"], manifest)
        assert "STOP_NEEDS_ORCHESTRATOR" in text
        assert "pyproject.toml" in text


def test_stop_token_is_recognised_in_a_run_result():
    assert RunResult(0, "I cannot proceed.\nSTOP_NEEDS_ORCHESTRATOR\nWHAT IS MISSING: a decision").stopped
    assert not RunResult(0, "done").stopped


def test_provenance_never_claims_a_review_that_did_not_run(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    message = prompts.commit_message(tasks["T002"], "qwen-local", None, "aaa", "bbb", False)
    assert "Reviewed-By: none" in message
    assert "Human-Approved: false" in message
    assert "Implemented-By: qwen-local" in message
    assert "Requirement-Ids: NET-001, QR-014" in message


def test_provenance_records_a_review_that_did_run(config):
    tasks = load_tasks(config.repo("police").path, config.project.task_dirs)
    message = prompts.commit_message(tasks["T002"], "qwen-local", "vendor/frontier", "aaa", "bbb", True)
    assert "Reviewed-By: vendor/frontier" in message
    assert "Human-Approved: true" in message


# -------------------------------------------------------------- pi json mode


def test_pi_json_stream_parsing_extracts_text_and_usage():
    stream = "\n".join([
        '{"type":"session","version":3}',
        '{"type":"message_update","usage":{"input":10,"output":2,"cacheRead":0,"cacheWrite":0,'
        '"totalTokens":12,"cost":{"input":0.1,"output":0.2,"cacheRead":0,"cacheWrite":0,"total":0.3}}}',
        '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"the plan"}]}}',
        "not json at all",
    ])
    result = parse_pi_json(stream)
    assert result.text == "the plan"
    assert (result.input_tokens, result.output_tokens) == (10, 2)
    assert result.reported_cost_usd == 0.3


def test_pi_json_without_usage_leaves_cost_unreported():
    result = parse_pi_json('{"type":"message_end","message":{"role":"assistant","content":"hi"}}')
    assert result.reported_cost_usd is None
    assert result.text == "hi"


def test_fake_runner_never_touches_a_network(config):
    runner = FakeRunner(RunResult(0, "fake output"))
    router = ModelRouter(config.models, ENV_PAID)
    choice = router._choice("r", "local-qwen", "plan", None)
    result = runner.run(choice, __import__("pathlib").Path("/tmp/p.md"), "plan", config.workspace)
    assert result.text == "fake output"
    assert runner.calls == [
        {
            "stage": "plan",
            "model": "local-qwen",
            "cwd": str(config.workspace),
            "policy_root": None,
            "policy_write_set": None,
        }
    ]
