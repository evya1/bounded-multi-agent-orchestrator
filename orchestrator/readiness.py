"""Exact project readiness semantics.

The rule, and nothing more::

    all depends_on tasks are done
    AND no unresolved gate has  blocks: start

``blocks: criterion`` and ``blocks: integration`` do NOT make a task unready.
They constrain a specific acceptance criterion or a later integration gate, and
are checked when that criterion or gate is reached — not at claim time.

Separately, and NOT part of project readiness, the harness enforces runtime
write-conflict safety: two concurrently active tasks in the same repository must
not own overlapping write paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Reason, Refusal
from .registers import GateState, Registers
from .task_loader import Gate, Task


@dataclass(frozen=True)
class GateVerdict:
    """One gate's derived state and what it actually blocks."""

    gate: Gate
    state: GateState
    explanation: str

    @property
    def blocks_claim(self) -> bool:
        """Only a start-scoped gate that is not RESOLVED can block a claim.

        UNKNOWN counts as blocking: we refuse rather than guess.
        """
        return self.gate.blocks_start and self.state is not GateState.RESOLVED

    def as_dict(self) -> dict:
        return {
            **self.gate.as_dict(),
            "state": str(self.state),
            "explanation": self.explanation,
            "blocks_claim": self.blocks_claim,
        }


@dataclass
class Readiness:
    """Whether one task is formally ready, and precisely why not."""

    task_id: str
    ready: bool
    gate_verdicts: list[GateVerdict] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    unmet_dependencies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "ready": self.ready,
            "unmet_dependencies": self.unmet_dependencies,
            "gates": [verdict.as_dict() for verdict in self.gate_verdicts],
            "refusals": [refusal.as_dict() for refusal in self.refusals],
        }


DONE_STATUSES = frozenset({"done"})


def evaluate_gates(task: Task, registers: Registers) -> list[GateVerdict]:
    """Derive every declared gate's state from the project's own registers."""
    verdicts = []
    for gate in task.gates:
        state, explanation = registers.gate_state(gate.id, gate.kind)
        verdicts.append(GateVerdict(gate, state, explanation))
    return verdicts


def evaluate(task: Task, tasks: dict[str, Task], registers: Registers) -> Readiness:
    """Apply the exact readiness rule to one task."""
    refusals: list[Refusal] = []

    unmet = [
        dependency
        for dependency in task.depends_on
        if (tasks.get(dependency) is None or tasks[dependency].status not in DONE_STATUSES)
    ]
    if unmet:
        refusals.append(
            Refusal(
                Reason.DEPENDENCY_INCOMPLETE,
                f"{task.id}: depends_on not done: {unmet}",
                {"unmet": unmet},
            )
        )

    verdicts = evaluate_gates(task, registers)
    for verdict in verdicts:
        if not verdict.blocks_claim:
            continue
        reason = (
            Reason.UNKNOWN_GATE_STATE
            if verdict.state is GateState.UNKNOWN
            else Reason.START_GATE_UNRESOLVED
        )
        refusals.append(
            Refusal(
                reason,
                f"{task.id}: start-blocking gate {verdict.gate.id} is {verdict.state}. "
                f"{verdict.explanation}",
                {"gate": verdict.gate.as_dict(), "state": str(verdict.state)},
            )
        )

    return Readiness(
        task_id=task.id,
        ready=not refusals,
        gate_verdicts=verdicts,
        refusals=refusals,
        unmet_dependencies=unmet,
    )


def post_start_gates(task: Task, registers: Registers) -> list[GateVerdict]:
    """Gates that constrain the task AFTER it may start.

    The start rule is deliberately untouched. This is the other half of the
    lifecycle: ``blocks: criterion`` and ``blocks: integration`` gates never
    prevent claiming, planning, implementing or verifying, but they do have to
    be evaluated before the work is represented as closed.
    """
    return [v for v in evaluate_gates(task, registers) if v.gate.blocks in ("criterion", "integration")]


def criterion_pending(task: Task, registers: Registers) -> list[Refusal]:
    """Unresolved criterion gates, reported as PENDING — never as blockers.

    A ``blocks: criterion`` gate constrains exactly one acceptance criterion. It
    does not block claiming, planning, implementing, verifying, reviewing, or
    preparing a local candidate PR. What it blocks is the *claim that the named
    criterion is satisfied*, and therefore any future close-the-task operation.

    These are carried as structured pending status so a later task-close
    operation can enforce them. UNKNOWN counts as unresolved: we never guess a
    criterion closed.
    """
    refusals = []
    for verdict in post_start_gates(task, registers):
        if verdict.gate.blocks != "criterion" or verdict.state is GateState.RESOLVED:
            continue
        anchor = verdict.gate.scope
        known = anchor in task.anchors if anchor else False
        refusals.append(
            Refusal(
                Reason.CRITERION_GATE_UNRESOLVED,
                f"{task.id}: acceptance criterion {verdict.gate.id} is NOT satisfied — gate is {verdict.state} "
                f"(scope={anchor!r}"
                + (f", anchor {{#{anchor}}} present in the task" if known else ", no matching anchor in the task body")
                + f"). Local work and a candidate PR may proceed; this criterion may not be "
                f"closed and the task may not be marked done. {verdict.explanation}",
                {
                    "gate": verdict.gate.as_dict(),
                    "state": str(verdict.state),
                    "criterion_anchor": anchor,
                    "anchor_found": known,
                },
            )
        )
    return refusals


def integration_pending(task: Task, registers: Registers) -> list[Refusal]:
    """Unresolved integration gates, surfaced but never treated as passed.

    Like criterion gates these never block local work or a candidate PR. v4 has
    no autonomous merge/integration stage, so they cannot be enforced here; they
    are reported so a candidate PR is never mistaken for an integrated one. A
    future merge/integration command MUST enforce them.
    """
    return [
        Refusal(
            Reason.INTEGRATION_GATE_PENDING,
            f"{task.id}: integration gate {v.gate.id} is {v.state} (scope={v.gate.scope!r}). "
            "Local work and a candidate PR may proceed; integration has NOT passed. "
            "A future merge/integration command must enforce this gate.",
            {"gate": v.gate.as_dict(), "state": str(v.state)},
        )
        for v in post_start_gates(task, registers)
        if v.gate.blocks == "integration" and v.state is not GateState.RESOLVED
    ]


def pending_project_gates(task: Task, registers: Registers) -> dict[str, list[dict]]:
    """Structured pending post-start gates, for status and provenance.

    Deliberately small: two lists of gate facts. It exists so that PR_READY can
    be recorded without ever implying the gated criterion passed, and so a
    future close/merge operation has something exact to enforce.
    """
    return {
        "criterion": [refusal.detail["gate"] | {"state": refusal.detail["state"]}
                      for refusal in criterion_pending(task, registers)],
        "integration": [refusal.detail["gate"] | {"state": refusal.detail["state"]}
                        for refusal in integration_pending(task, registers)],
    }


def format_pending(gates: dict[str, list[dict]]) -> list[str]:
    """One short line per pending gate, for human output."""
    return [
        f"{level}: {gate['id']} (scope={gate.get('scope')!r}, {gate['state']})"
        for level, entries in gates.items()
        for gate in entries
    ]


def ready_tasks(tasks: dict[str, Task], registers: Registers) -> list[Readiness]:
    """Readiness for every task that is not already done, in task-ID order."""
    return [
        evaluate(task, tasks, registers)
        for _, task in sorted(tasks.items())
        if task.status not in DONE_STATUSES
    ]


# ------------------------------------------------------------- write conflicts


def _overlaps(left: str, right: str) -> bool:
    """Two write_set entries collide when either is a prefix directory of the other."""
    if left == right:
        return True
    left_prefix = left if left.endswith("/") else left + "/"
    right_prefix = right if right.endswith("/") else right + "/"
    return right.startswith(left_prefix) or left.startswith(right_prefix)


def write_conflicts(task: Task, active: dict[str, Task]) -> list[Refusal]:
    """Overlapping write ownership against other ACTIVE tasks in the same repo.

    Serialization is only proven when the other task is a ``depends_on`` ancestor
    that is already done — and a done task is not active, so it never appears
    here. Anything else overlapping is a refusal, whatever ``parallel_safe`` says:
    ``parallel_safe`` is a task-graph hint, not a runtime proof.
    """
    refusals = []
    for other_id, other in sorted(active.items()):
        if other_id == task.id:
            continue
        collisions = sorted(
            {
                (mine, theirs)
                for mine in task.write_set
                for theirs in other.write_set
                if _overlaps(mine, theirs)
            }
        )
        if collisions:
            refusals.append(
                Refusal(
                    Reason.WRITE_CONFLICT,
                    f"{task.id}: write_set overlaps active task {other_id}: {collisions}",
                    {"other_task": other_id, "collisions": collisions},
                )
            )
    return refusals
