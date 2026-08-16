"""Approvals bound to the exact artifact, repository, task, stage and base."""

from __future__ import annotations

from orchestrator.approvals import (
    ApprovalStore,
    diff_gate_required,
    required_gates,
    touches_governance,
)
from orchestrator.errors import Reason

PLAN = "1. create pyproject.toml\n2. run uv lock\n"


def test_approval_is_valid_for_the_exact_artifact(config):
    store = ApprovalStore(config)
    repo = config.repo("police")
    store.record(repo, "T002", "plan", "base1", PLAN, True, "looks right")
    assert store.check(repo, "T002", "plan", "base1", PLAN).valid is True


def test_one_byte_change_invalidates_the_approval(config):
    store = ApprovalStore(config)
    repo = config.repo("police")
    store.record(repo, "T002", "plan", "base1", PLAN, True)
    check = store.check(repo, "T002", "plan", "base1", PLAN + " ")
    assert check.valid is False
    assert check.refusal.reason is Reason.APPROVAL_ARTIFACT_MISMATCH


def test_police_approval_cannot_approve_thief(config):
    """Same task ID, same artifact bytes, different repository identity."""
    store = ApprovalStore(config)
    police, thief = config.repo("police"), config.repo("thief")
    store.record(police, "T002", "plan", "base1", PLAN, True)

    # thief has no receipt of its own
    assert store.check(thief, "T002", "plan", "base1", PLAN).refusal.reason is Reason.APPROVAL_MISSING

    # and a receipt physically copied across still fails on identity
    target = store.path_for(thief, "T002", "plan")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(store.path_for(police, "T002", "plan").read_text())
    check = store.check(thief, "T002", "plan", "base1", PLAN)
    assert check.valid is False
    assert check.refusal.reason is Reason.APPROVAL_REPO_MISMATCH


def test_approval_does_not_survive_a_changed_base(config):
    store = ApprovalStore(config)
    repo = config.repo("police")
    store.record(repo, "T002", "diff", "base1", PLAN, True)
    check = store.check(repo, "T002", "diff", "base2", PLAN)
    assert check.valid is False
    assert check.refusal.reason is Reason.APPROVAL_BASE_MISMATCH


def test_rejection_is_recorded_and_refuses(config):
    store = ApprovalStore(config)
    repo = config.repo("police")
    store.record(repo, "T002", "diff", "base1", PLAN, False, "test asserts nothing")
    check = store.check(repo, "T002", "diff", "base1", PLAN)
    assert check.valid is False
    assert check.refusal.reason is Reason.APPROVAL_REJECTED
    assert "test asserts nothing" in check.refusal.message


def test_a_plan_receipt_is_not_a_diff_receipt(config):
    store = ApprovalStore(config)
    repo = config.repo("police")
    store.record(repo, "T002", "plan", "base1", PLAN, True)
    assert store.check(repo, "T002", "diff", "base1", PLAN).refusal.reason is Reason.APPROVAL_MISSING


def test_receipts_live_outside_every_repository(config):
    """A model editing its write_set cannot reach an approval receipt."""
    store = ApprovalStore(config)
    repo = config.repo("police")
    path = store.path_for(repo, "T002", "plan")
    assert config.workspace in path.parents
    assert repo.path not in path.parents


# ------------------------------------------------------------- policy tables


def test_risk_policy_table(config):
    assert required_gates("high", config.review_policy) == {"plan", "diff"}
    assert required_gates("medium", config.review_policy) == {"diff"}
    assert required_gates("low", config.review_policy) == set()


def test_unknown_risk_gets_the_strictest_policy(config):
    assert required_gates("bananas", config.review_policy) == {"plan", "diff"}


def test_governance_paths_always_require_a_diff_gate(config):
    needed, why = diff_gate_required("low", ["docs/PRD.md"], config.review_policy, config.governance_paths)
    assert needed is True
    assert "governance" in why


def test_governance_directory_prefix_matches(config):
    assert touches_governance(["docs/spec/OPEN_QUESTIONS.md"], config.governance_paths) == [
        "docs/spec/OPEN_QUESTIONS.md"
    ]
    assert touches_governance(["docs/specific.md"], config.governance_paths) == []


def test_low_risk_non_governance_needs_no_diff_gate(config):
    needed, _ = diff_gate_required("low", ["src/core.py"], config.review_policy, config.governance_paths)
    assert needed is False
