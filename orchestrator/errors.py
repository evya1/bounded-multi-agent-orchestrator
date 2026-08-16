"""Machine-readable refusal and failure reasons.

Every refusal carries a stable ``reason`` code so a caller (human or script) can
branch on it without parsing prose. Nothing in this harness refuses silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Reason(StrEnum):
    """Stable machine-readable refusal codes."""

    # readiness / claiming
    DEPENDENCY_INCOMPLETE = "DEPENDENCY_INCOMPLETE"
    START_GATE_UNRESOLVED = "START_GATE_UNRESOLVED"
    UNKNOWN_GATE_STATE = "UNKNOWN_GATE_STATE"
    CRITERION_GATE_UNRESOLVED = "CRITERION_GATE_UNRESOLVED"
    INTEGRATION_GATE_PENDING = "INTEGRATION_GATE_PENDING"
    WRITE_CONFLICT = "WRITE_CONFLICT"
    RESOURCE_HELD = "RESOURCE_HELD"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_DONE = "TASK_DONE"

    # context
    CONTEXT_FILE_MISSING = "CONTEXT_FILE_MISSING"
    CONTEXT_ID_UNRESOLVED = "CONTEXT_ID_UNRESOLVED"
    CONTEXT_COMPILE_FAILED = "CONTEXT_COMPILE_FAILED"

    # approvals
    APPROVAL_MISSING = "APPROVAL_MISSING"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_ARTIFACT_MISMATCH = "APPROVAL_ARTIFACT_MISMATCH"
    APPROVAL_BASE_MISMATCH = "APPROVAL_BASE_MISMATCH"
    APPROVAL_REPO_MISMATCH = "APPROVAL_REPO_MISMATCH"
    APPROVAL_TASK_MISMATCH = "APPROVAL_TASK_MISMATCH"
    APPROVAL_STAGE_MISMATCH = "APPROVAL_STAGE_MISMATCH"

    # verification / write set
    OUT_OF_WRITE_SET = "OUT_OF_WRITE_SET"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_NOT_RUN = "VERIFICATION_NOT_RUN"
    VERIFICATION_SIDE_EFFECT = "VERIFICATION_SIDE_EFFECT"

    # worktree
    WORKTREE_STALE_BASE = "WORKTREE_STALE_BASE"
    WORKTREE_MISSING = "WORKTREE_MISSING"
    WORKTREE_BASE_MISMATCH = "WORKTREE_BASE_MISMATCH"
    TREE_NOT_PROVEN = "TREE_NOT_PROVEN"
    WRITE_GUARD_UNAVAILABLE = "WRITE_GUARD_UNAVAILABLE"

    # models / budget
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    BUDGET_REFUSED = "BUDGET_REFUSED"
    DIVERSITY_LOST = "DIVERSITY_LOST"
    SPEND_NOT_AUTHORIZED = "SPEND_NOT_AUTHORIZED"
    STOP_NEEDS_ORCHESTRATOR = "STOP_NEEDS_ORCHESTRATOR"

    # state machine
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"

    # configuration
    CONFIG_INVALID = "CONFIG_INVALID"
    REPO_UNKNOWN = "REPO_UNKNOWN"


@dataclass(frozen=True)
class Refusal:
    """One machine-readable refusal with a human-actionable message."""

    reason: Reason
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"reason": str(self.reason), "message": self.message, "detail": self.detail}

    def __str__(self) -> str:
        return f"{self.reason}: {self.message}"


class OrchestratorError(Exception):
    """Raised for an unrecoverable, operator-actionable harness failure."""

    def __init__(self, reason: Reason, message: str, detail: dict | None = None) -> None:
        super().__init__(f"{reason}: {message}")
        self.refusal = Refusal(reason, message, detail or {})
