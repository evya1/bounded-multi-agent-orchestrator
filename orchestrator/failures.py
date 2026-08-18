"""Deterministic failure classification and retry policy.

A retry decision must never be a judgement call made in the moment. Each way a
bounded run can fail maps to exactly one class, and each class carries exactly
one policy. That is what stops the two expensive patterns:

* the same failing task being re-dispatched over and over because each attempt
  "looked transient";
* a writer and a reviewer arguing across many paid rounds because nothing
  decided when the argument had to stop.

The normal workflow permits ONE implementation correction cycle. Not two, not
"one more to be safe".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .errors import Reason
from .pi_rpc import Lifecycle, StopReason, WorkerOutcome


class FailureClass(StrEnum):
    """Every way a bounded run can fail to produce usable work."""

    NONE = "NONE"
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    LOCAL_SERVER_DOWN = "LOCAL_SERVER_DOWN"
    TOOL_ERROR = "TOOL_ERROR"
    MALFORMED_RESULT = "MALFORMED_RESULT"
    TARGETED_TEST_FAILED = "TARGETED_TEST_FAILED"
    WRITE_SET_VIOLATION = "WRITE_SET_VIOLATION"
    REVIEW_BLOCKER_SIMPLE = "REVIEW_BLOCKER_SIMPLE"
    ARCHITECTURAL_DISAGREEMENT = "ARCHITECTURAL_DISAGREEMENT"
    QUIET_WORKER = "QUIET_WORKER"
    TIMEOUT = "TIMEOUT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    PROCESS_LOST = "PROCESS_LOST"
    WORKER_BLOCKED = "WORKER_BLOCKED"


class Action(StrEnum):
    """What the workflow does next. There is no 'try again and hope'."""

    CONTINUE = "CONTINUE"
    RETRY_SAME_MODEL = "RETRY_SAME_MODEL"
    REPAIR_FORMAT = "REPAIR_FORMAT"
    CHEAP_FIXER = "CHEAP_FIXER"
    RESOLVER = "RESOLVER"
    FAIL_CLOSED = "FAIL_CLOSED"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"


@dataclass(frozen=True)
class Policy:
    """The single, fixed response to one failure class."""

    failure: FailureClass
    action: Action
    max_attempts: int
    substitute_model: bool
    explanation: str
    reason: Reason | None = None

    def as_dict(self) -> dict:
        return {
            "failure": str(self.failure),
            "action": str(self.action),
            "max_attempts": self.max_attempts,
            "substitute_model": self.substitute_model,
            "explanation": self.explanation,
            "reason": str(self.reason) if self.reason else None,
        }


#: The taxonomy. This table IS the retry policy; nothing else may invent one.
POLICIES: dict[FailureClass, Policy] = {
    FailureClass.NONE: Policy(
        FailureClass.NONE, Action.CONTINUE, 0, False, "no failure; proceed"
    ),
    FailureClass.TRANSIENT_PROVIDER: Policy(
        FailureClass.TRANSIENT_PROVIDER,
        Action.RETRY_SAME_MODEL,
        1,
        False,
        "retry the SAME exact model once; a transient 5xx is not a reason to change model",
    ),
    FailureClass.MODEL_UNAVAILABLE: Policy(
        FailureClass.MODEL_UNAVAILABLE,
        Action.FAIL_CLOSED,
        0,
        False,
        "fail closed; silently substituting another model family changes who did the work",
        Reason.MODEL_UNAVAILABLE,
    ),
    FailureClass.LOCAL_SERVER_DOWN: Policy(
        FailureClass.LOCAL_SERVER_DOWN,
        Action.FAIL_CLOSED,
        0,
        False,
        "mark the local model unavailable; do not auto-start a GPU server and do not "
        "silently move the work to a paid OpenRouter model",
        Reason.MODEL_UNAVAILABLE,
    ),
    FailureClass.TOOL_ERROR: Policy(
        FailureClass.TOOL_ERROR,
        Action.FAIL_CLOSED,
        0,
        False,
        "report the exact tool failure; do not re-run the whole task to see if it recurs",
    ),
    FailureClass.MALFORMED_RESULT: Policy(
        FailureClass.MALFORMED_RESULT,
        Action.REPAIR_FORMAT,
        1,
        False,
        "one bounded format-repair request; never re-run the implementation for a syntax slip",
    ),
    FailureClass.TARGETED_TEST_FAILED: Policy(
        FailureClass.TARGETED_TEST_FAILED,
        Action.CHEAP_FIXER,
        1,
        False,
        "hand the exact deterministic failure output to the cheap fixer; one correction cycle",
        Reason.VERIFICATION_FAILED,
    ),
    FailureClass.WRITE_SET_VIOLATION: Policy(
        FailureClass.WRITE_SET_VIOLATION,
        Action.FAIL_CLOSED,
        0,
        False,
        "a change outside the declared write set blocks progression; review is not reached",
        Reason.OUT_OF_WRITE_SET,
    ),
    FailureClass.REVIEW_BLOCKER_SIMPLE: Policy(
        FailureClass.REVIEW_BLOCKER_SIMPLE,
        Action.CHEAP_FIXER,
        1,
        False,
        "a straightforward implementation bug is repaired once by the cheap fixer",
    ),
    FailureClass.ARCHITECTURAL_DISAGREEMENT: Policy(
        FailureClass.ARCHITECTURAL_DISAGREEMENT,
        Action.RESOLVER,
        1,
        False,
        "invoke the resolver exactly ONCE; a second opinion on a second opinion is a debate",
    ),
    FailureClass.QUIET_WORKER: Policy(
        FailureClass.QUIET_WORKER,
        Action.CONTINUE,
        0,
        False,
        "silence is not failure; the supervisor consults Pi's protocol state before acting",
    ),
    FailureClass.TIMEOUT: Policy(
        FailureClass.TIMEOUT,
        Action.FAIL_CLOSED,
        0,
        False,
        "the worker was aborted at its hard deadline; the state is persisted for a human",
    ),
    FailureClass.BUDGET_EXCEEDED: Policy(
        FailureClass.BUDGET_EXCEEDED,
        Action.HUMAN_APPROVAL,
        0,
        False,
        "no further paid generation is dispatched without explicit human approval",
        Reason.BUDGET_REFUSED,
    ),
    FailureClass.PROCESS_LOST: Policy(
        FailureClass.PROCESS_LOST,
        Action.HUMAN_APPROVAL,
        0,
        False,
        "the process vanished before settling; a human decides whether to re-dispatch, "
        "because an automatic re-run risks duplicating work that partly happened",
    ),
    FailureClass.WORKER_BLOCKED: Policy(
        FailureClass.WORKER_BLOCKED,
        Action.HUMAN_APPROVAL,
        0,
        False,
        "the worker honestly reported it is blocked; an honest stop is a result, not a retry",
    ),
}


def policy_for(failure: FailureClass) -> Policy:
    return POLICIES[failure]


#: Substrings that identify a genuinely transient provider condition. Kept
#: narrow on purpose: a broad match would turn every error into a paid retry.
_TRANSIENT_MARKERS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "429",
    "500 ",
    "502",
    "503",
    "504",
    "529",
    "temporarily unavailable",
    "connection reset",
)


def classify_outcome(outcome: WorkerOutcome, stderr_tail: str = "") -> FailureClass:
    """Classify a finished worker run. Lifecycle first, prose never.

    The lifecycle answers most of this by itself, which is the point: the
    orchestrator is not reading error messages to guess whether the worker is
    still alive.
    """
    lifecycle = Lifecycle(outcome.lifecycle)
    stop = outcome.stop_reason

    if lifecycle == Lifecycle.COMPLETE:
        return FailureClass.NONE
    if lifecycle == Lifecycle.BUDGET_EXCEEDED or stop == str(StopReason.HARD_BUDGET):
        return FailureClass.BUDGET_EXCEEDED
    if lifecycle == Lifecycle.TIMED_OUT or stop in (
        str(StopReason.WALL_DEADLINE),
        str(StopReason.INACTIVITY),
    ):
        return FailureClass.TIMEOUT
    if lifecycle == Lifecycle.ABORTED or stop in (
        str(StopReason.MAX_MODEL_CALLS),
        str(StopReason.MAX_TOOL_CALLS),
    ):
        return FailureClass.TIMEOUT
    if lifecycle == Lifecycle.PROCESS_LOST or stop == str(StopReason.PROCESS_EXITED):
        haystack = f"{outcome.detail} {stderr_tail}".lower()
        if any(marker in haystack for marker in _TRANSIENT_MARKERS):
            return FailureClass.TRANSIENT_PROVIDER
        return FailureClass.PROCESS_LOST
    if lifecycle == Lifecycle.FAILED or stop == str(StopReason.SPAWN_FAILED):
        haystack = f"{outcome.detail} {stderr_tail}".lower()
        if "econnrefused" in haystack or "connection refused" in haystack:
            return FailureClass.LOCAL_SERVER_DOWN
        return FailureClass.MODEL_UNAVAILABLE
    return FailureClass.PROCESS_LOST


def classify_review(blocking: list[str], text: str = "") -> FailureClass:
    """Is a reviewer's blocking finding a bug to fix, or a decision to resolve?

    A code bug goes to the cheap fixer. A disagreement about architecture or an
    ambiguous requirement goes to the resolver, ONCE. Everything else stays a
    simple blocker, because the cheap path is the correct default.
    """
    if not blocking:
        return FailureClass.NONE
    haystack = " ".join([*blocking, text]).lower()
    architectural = (
        "architectur",
        "design decision",
        "ambiguous requirement",
        "requirement conflict",
        "contradicts the contract",
        "contract violation",
        "wrong abstraction",
        "adr",
        "should be decided",
        "out of scope for this task",
    )
    if any(marker in haystack for marker in architectural):
        return FailureClass.ARCHITECTURAL_DISAGREEMENT
    return FailureClass.REVIEW_BLOCKER_SIMPLE


def render_taxonomy() -> str:
    """Human-readable dump of the whole policy table, for docs and `doctor`."""
    lines = ["FAILURE / RETRY TAXONOMY", ""]
    for failure, policy in POLICIES.items():
        if failure == FailureClass.NONE:
            continue
        lines.append(f"  {failure!s:<28} -> {policy.action!s:<18} max={policy.max_attempts}")
        lines.append(f"      {policy.explanation}")
    return "\n".join(lines)
