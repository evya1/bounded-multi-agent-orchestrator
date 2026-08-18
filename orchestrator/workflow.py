"""The deterministic workflow.

This module is the orchestrator in the literal sense: it decides what happens
next. Nothing here asks a model what to do. Every transition is taken by Python
on evidence it can prove — a Pi protocol event, a Git diff, an exit code, a
budget arithmetic.

    PREFLIGHT -> CONTEXT -> WRITER -> WRITE-SET AUDIT -> LEVEL 1
              -> [FIXER] -> LEVEL 2 -> REVIEW PACKET -> REVIEWER
              -> [FIXER] -> [RESOLVER] -> LEVEL 3 -> HUMAN MERGE GATE

Square brackets are conditional and each is entered AT MOST ONCE. A green run
touches neither: writer, deterministic gates, one independent reviewer, human.

Workers are dispatched through an injected ``Dispatcher``. Production hands it a
real ``pi --mode rpc`` child process; tests hand it a scripted one. The state
machine cannot tell the difference, which is the point — the logic that decides
whether to spend money is tested without spending any.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import budget as budget_mod
from . import review_packet as review_packet_mod
from .errors import OrchestratorError, Reason, Refusal
from .failures import Action, FailureClass, classify_outcome, classify_review, policy_for
from .model_router import ModelChoice, ModelRouter, validate_no_claude_runtime
from .pi_rpc import Lifecycle, RoleLimits, WorkerOutcome
from .task_loader import Task
from .worker_result import (
    ParseOutcome,
    ReviewerResult,
    WorkerResult,
    parse_reviewer_result,
    parse_worker_result,
)


class Phase(StrEnum):
    """Where the whole task run is. Persisted, so a restart resumes here."""

    PREFLIGHT = "PREFLIGHT"
    CONTEXT = "CONTEXT"
    WRITER = "WRITER"
    WRITE_SET_AUDIT = "WRITE_SET_AUDIT"
    LEVEL1 = "LEVEL1"
    FIX = "FIX"
    LEVEL2 = "LEVEL2"
    REVIEW_PACKET = "REVIEW_PACKET"
    REVIEW = "REVIEW"
    RESOLVE = "RESOLVE"
    LEVEL3 = "LEVEL3"
    HUMAN_MERGE_GATE = "HUMAN_MERGE_GATE"
    BLOCKED = "BLOCKED"


class ValidationLevel(StrEnum):
    """Three levels, run at three different moments, for three different costs."""

    #: Immediately after a bounded edit. Seconds, targeted at what changed.
    LEVEL1 = "LEVEL1_TARGETED"
    #: Before the expensive reviewer. The component's own suite plus checkers.
    LEVEL2 = "LEVEL2_COMPONENT"
    #: Once, near integration. The full repository gate set.
    LEVEL3 = "LEVEL3_INTEGRATION"


@dataclass
class StageRecord:
    """One completed stage, with everything needed to explain it afterwards."""

    phase: str
    role: str = ""
    model: str = ""
    provider: str = ""
    lifecycle: str = ""
    stop_reason: str = ""
    passed: bool = False
    failure: str = str(FailureClass.NONE)
    action: str = str(Action.CONTINUE)
    cost_usd: float = 0.0
    cost_source: str = ""
    detail: str = ""
    artifacts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "phase": self.phase,
            "role": self.role,
            "model": self.model,
            "provider": self.provider,
            "lifecycle": self.lifecycle,
            "stop_reason": self.stop_reason,
            "passed": self.passed,
            "failure": self.failure,
            "action": self.action,
            "cost_usd": round(self.cost_usd, 6),
            "cost_source": self.cost_source,
            "detail": self.detail,
            "artifacts": self.artifacts,
        }


@dataclass
class WorkflowReport:
    """The whole run, as one inspectable object."""

    repo: str
    task_id: str
    task_run_id: str
    phase: str = str(Phase.PREFLIGHT)
    stages: list[StageRecord] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    fixer_cycles: int = 0
    resolver_cycles: int = 0
    total_cost_usd: float = 0.0
    human_gate: str = ""
    ready_for_human: bool = False

    def record(self, stage: StageRecord) -> StageRecord:
        self.stages.append(stage)
        self.total_cost_usd = round(self.total_cost_usd + stage.cost_usd, 6)
        return stage

    def refuse(self, refusal: Refusal) -> None:
        self.refusals.append(refusal)
        self.phase = str(Phase.BLOCKED)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "task_id": self.task_id,
            "task_run_id": self.task_run_id,
            "phase": self.phase,
            "ready_for_human": self.ready_for_human,
            "human_gate": self.human_gate,
            "fixer_cycles": self.fixer_cycles,
            "resolver_cycles": self.resolver_cycles,
            "total_cost_usd": self.total_cost_usd,
            "stages": [stage.as_dict() for stage in self.stages],
            "refusals": [refusal.as_dict() for refusal in self.refusals],
        }


# ------------------------------------------------------------------ dispatch


@dataclass
class Dispatch:
    """One bounded worker request. Everything the supervisor needs, and no more."""

    stage: str
    role: str
    choice: ModelChoice
    limits: RoleLimits
    prompt: str
    worker_id: str
    read_only: bool


#: A dispatcher runs ONE bounded worker and returns its supervised outcome.
Dispatcher = Callable[[Dispatch], WorkerOutcome]

#: A validator runs one deterministic validation level and returns
#: (passed, output). It is Python and exit codes, never a model.
Validator = Callable[[ValidationLevel], tuple[bool, str]]

#: An auditor answers "did the change stay inside the write set?" from Git.
Auditor = Callable[[], tuple[bool, str]]


@dataclass
class DryRunPlan:
    """Everything a real run WOULD do, resolved without spending anything."""

    repo: str
    task_id: str
    complexity: str
    routing: dict = field(default_factory=dict)
    availability: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    write_set: list[str] = field(default_factory=list)
    validation_plan: dict = field(default_factory=dict)
    human_gates: list[str] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        return not self.refusals

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "task_id": self.task_id,
            "complexity": self.complexity,
            "executable": self.executable,
            "routing": self.routing,
            "availability": self.availability,
            "budgets": self.budgets,
            "context": self.context,
            "write_set": self.write_set,
            "validation_plan": self.validation_plan,
            "human_gates": self.human_gates,
            "notes": self.notes,
            "refusals": [refusal.as_dict() for refusal in self.refusals],
        }

    def render(self) -> str:
        lines = [
            f"DRY RUN — {self.repo}/{self.task_id}  (complexity={self.complexity})",
            "Nothing below was executed. No paid model was called, no file was changed,",
            "no commit was made.",
            "",
            "ROUTING",
        ]
        for stage, row in self.routing.items():
            # A conditional stage with no role at this complexity is normal —
            # `resolve` is deliberately unconfigured below high risk — so it is
            # rendered as such rather than crashing the report.
            role = row.get("role") or "(not configured)"
            model = row.get("model") or "-"
            lines.append(
                f"  {stage:<10} role={role:<18} {model:<22} {row.get('presence') or '-'}"
            )
        lines += ["", "BUDGETS"]
        total = self.budgets.get("total") or {}
        lines.append(
            f"  run              soft ${total.get('soft_usd', 0):.2f}  hard ${total.get('hard_usd', 0):.2f}"
        )
        for role, row in (self.budgets.get("roles") or {}).items():
            lines.append(
                f"  role:{role:<12} soft ${row.get('soft_usd', 0):.2f}  hard ${row.get('hard_usd', 0):.2f}"
            )
        lines += ["", "CONTEXT PACKET"]
        lines.append(
            f"  {self.context.get('files', 0)} file(s), {self.context.get('bytes', 0)} bytes, "
            f"~{self.context.get('estimated_tokens', 0)} tokens"
        )
        lines += ["", "WRITE SET"]
        lines += [f"  {path}" for path in self.write_set] or ["  (empty)"]
        lines += ["", "VALIDATION PLAN"]
        for level, commands in self.validation_plan.items():
            lines.append(f"  {level}")
            lines += [f"    $ {command}" for command in commands] or ["    (none declared)"]
        lines += ["", "HUMAN GATES"]
        lines += [f"  {gate}" for gate in self.human_gates] or ["  (none for this risk level)"]
        for note in self.notes:
            lines.append(f"NOTE  {note}")
        if self.refusals:
            lines += ["", "THIS RUN WOULD REFUSE:"]
            lines += [f"  - {refusal}" for refusal in self.refusals]
        return "\n".join(lines)


@dataclass(frozen=True)
class RunSpec:
    """One task run, declared in ``config/runs/<task>.yaml``.

    A run spec names the task and the validation commands for each level. It
    does NOT name models: routing is policy and lives in the routing table, so
    a run cannot quietly promote itself onto a more expensive model.
    """

    repo: str
    task: str
    complexity: str | None = None
    level1: tuple[str, ...] = ()
    level2: tuple[str, ...] = ()
    level3: tuple[str, ...] = ()
    base: str | None = None
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> RunSpec:
        import yaml

        if not path.is_file():
            raise OrchestratorError(Reason.CONFIG_INVALID, f"no such run config: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise OrchestratorError(Reason.CONFIG_INVALID, f"{path}: top level must be a mapping")
        for required in ("repo", "task"):
            if not raw.get(required):
                raise OrchestratorError(
                    Reason.CONFIG_INVALID, f"{path}: '{required}' is required"
                )
        if "model" in raw or "models" in raw or "provider" in raw:
            raise OrchestratorError(
                Reason.CONFIG_INVALID,
                f"{path}: a run config may not name a model or provider. Routing is policy "
                "and lives in the routing table, so a run cannot promote itself to a more "
                "expensive model.",
            )
        validation = raw.get("validation") or {}
        return cls(
            repo=str(raw["repo"]),
            task=str(raw["task"]),
            complexity=raw.get("complexity"),
            level1=tuple(validation.get("level1") or ()),
            level2=tuple(validation.get("level2") or ()),
            level3=tuple(validation.get("level3") or ()),
            base=raw.get("base"),
            notes=str(raw.get("notes", "")),
        )

    def commands_for(self, level: ValidationLevel, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
        declared = {
            ValidationLevel.LEVEL1: self.level1,
            ValidationLevel.LEVEL2: self.level2,
            ValidationLevel.LEVEL3: self.level3,
        }[level]
        return declared or fallback

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "task": self.task,
            "complexity": self.complexity,
            "base": self.base,
            "validation": {
                "level1": list(self.level1),
                "level2": list(self.level2),
                "level3": list(self.level3),
            },
            "notes": self.notes,
        }


class WorkflowEngine:
    """Drives one bounded task run from preflight to the human merge gate."""

    def __init__(
        self,
        *,
        repo_name: str,
        task: Task,
        task_run_id: str,
        router: ModelRouter,
        run_budget: budget_mod.RunBudget,
        dispatcher: Dispatcher,
        validator: Validator,
        auditor: Auditor,
        complexity: str | None = None,
        on_stage: Callable[[StageRecord], None] | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.task = task
        self.task_run_id = task_run_id
        self.router = router
        self.run_budget = run_budget
        self.dispatcher = dispatcher
        self.validator = validator
        self.auditor = auditor
        self.complexity = complexity or task.risk
        self.on_stage = on_stage or (lambda stage: None)
        self.report = WorkflowReport(repo_name, task.id, task_run_id)

    # -- preflight ---------------------------------------------------------

    def preflight(self) -> list[Refusal]:
        """Everything that must be true before any worker is spawned."""
        refusals: list[Refusal] = []
        refusals += validate_no_claude_runtime(self.router.config)
        for stage in ("implement", "review"):
            role = self.router.role_for(stage, self.complexity)
            if role is None:
                refusals.append(
                    Refusal(
                        Reason.CONFIG_INVALID,
                        f"no role configured for stage={stage!r} complexity={self.complexity!r}; "
                        "an unconfigured stage fails closed rather than borrowing another "
                        "role's model and budget",
                        {"stage": stage, "complexity": self.complexity},
                    )
                )
                continue
            choice, stage_refusals, _ = self.router.resolve(stage, self.complexity)
            if choice is None:
                refusals += stage_refusals
        return refusals

    def resolve_stage(self, stage: str) -> tuple[ModelChoice | None, list[Refusal], list[str]]:
        return self.router.resolve(stage, self.complexity)

    # -- one bounded worker ------------------------------------------------

    def _run_worker(self, phase: Phase, stage: str, prompt: str) -> tuple[StageRecord, WorkerOutcome | None]:
        """Resolve, authorize, dispatch and classify exactly one bounded worker."""
        record = StageRecord(phase=str(phase))
        choice, refusals, notes = self.resolve_stage(stage)
        if choice is None:
            record.failure = str(FailureClass.MODEL_UNAVAILABLE)
            record.action = str(Action.FAIL_CLOSED)
            record.detail = "; ".join(refusal.message for refusal in refusals)
            for refusal in refusals:
                self.report.refuse(refusal)
            return self.report.record(record), None

        record.role, record.model, record.provider = choice.role, choice.name, choice.provider
        limits = self.router.limits_for(choice.role)

        # The budget question is asked BEFORE dispatch, and it is forward
        # looking: not "have we overspent?" but "could one more call overspend?"
        expected = _expected_cost(choice, prompt)
        verdict = self.run_budget.authorize_generation(choice.role, expected, paid=choice.paid)
        for warning in verdict.warnings:
            notes.append(warning)
        if not verdict.allowed and verdict.refusal is not None:
            record.failure = str(FailureClass.BUDGET_EXCEEDED)
            record.action = str(Action.HUMAN_APPROVAL)
            record.detail = verdict.refusal.message
            self.report.refuse(verdict.refusal)
            return self.report.record(record), None

        read_only = (choice.privileges or {}).get("tools", "none") == "none"
        outcome = self.dispatcher(
            Dispatch(
                stage=stage,
                role=choice.role,
                choice=choice,
                limits=limits,
                prompt=prompt,
                worker_id=f"{self.task_run_id}:{stage}",
                read_only=read_only,
            )
        )
        record.lifecycle = outcome.lifecycle
        record.stop_reason = outcome.stop_reason
        record.cost_usd = outcome.telemetry.cost_usd
        record.cost_source = outcome.telemetry.cost_source
        failure = classify_outcome(outcome)
        record.failure = str(failure)
        record.action = str(policy_for(failure).action)
        record.passed = failure == FailureClass.NONE
        record.detail = outcome.detail
        if not record.passed:
            self.report.refuse(
                Refusal(
                    policy_for(failure).reason or Reason.STOP_NEEDS_ORCHESTRATOR,
                    f"{stage}: {failure} — {policy_for(failure).explanation}",
                    {"lifecycle": outcome.lifecycle, "stop_reason": outcome.stop_reason},
                )
            )
        stage_record = self.report.record(record)
        self.on_stage(stage_record)
        return stage_record, outcome

    def parse_result(
        self, outcome: WorkerOutcome, reviewer: bool = False
    ) -> tuple[ParseOutcome, bool]:
        """Parse a worker's semantic block, allowing ONE bounded format repair.

        Returns (parse outcome, whether a repair was needed). The repair asks
        only for the block; it never re-runs the work, because a missing brace
        is not a reason to pay for an implementation twice.
        """
        parse = parse_reviewer_result if reviewer else parse_worker_result
        first = parse(outcome.final_text or "")
        return first, not first.ok


# --------------------------------------------------------------- estimation


def _expected_cost(choice: ModelChoice, prompt: str, output_tokens: int = 4000) -> float:
    """Pre-call estimate. Catalog arithmetic, explicitly not an invoice.

    Used only to answer "could this call cross a hard budget?". The recorded
    cost of a call that actually happens comes from the provider whenever Pi
    reports it.
    """
    if not choice.paid:
        return 0.0
    input_tokens = max(1, len(prompt.encode()) // 4)
    return budget_mod.estimate_cost(choice.price, input_tokens, output_tokens)


def build_review_packet(**kwargs) -> review_packet_mod.ReviewPacket:
    """Re-exported so the workflow is the single place a packet is assembled."""
    return review_packet_mod.build(**kwargs)


def next_after_review(
    result: ReviewerResult, fixer_cycles: int, resolver_cycles: int
) -> tuple[Phase, FailureClass, str]:
    """The review routing rule, in one place and with hard cycle caps.

    A blocking finding does NOT automatically summon the resolver. A code bug
    goes to the cheap fixer; only a genuine architectural or requirement
    disagreement goes to the resolver, and only once.
    """
    if result.approved and not result.blocking:
        return Phase.LEVEL3, FailureClass.NONE, "reviewer approved with no blocking findings"

    failure = classify_review(result.blocking, " ".join(result.non_blocking))
    if failure == FailureClass.ARCHITECTURAL_DISAGREEMENT:
        if resolver_cycles >= 1:
            return (
                Phase.BLOCKED,
                failure,
                "the resolver has already answered once; a second resolver call would be a "
                "debate, not a decision. This needs a human.",
            )
        return Phase.RESOLVE, failure, "material disagreement: resolver, exactly once"
    if fixer_cycles >= 1:
        return (
            Phase.BLOCKED,
            failure,
            "one correction cycle has already been spent; a second is not automatic. "
            "This needs a human.",
        )
    return Phase.FIX, failure, "straightforward blocker: cheap fixer, exactly once"


def summarise(report: WorkflowReport) -> str:
    lines = [
        f"WORKFLOW {report.repo}/{report.task_id}",
        f"  run id      {report.task_run_id}",
        f"  phase       {report.phase}",
        f"  cost        ${report.total_cost_usd:.4f}",
        f"  fix cycles  {report.fixer_cycles}    resolver cycles {report.resolver_cycles}",
        "",
    ]
    for stage in report.stages:
        mark = "ok  " if stage.passed else "FAIL"
        lines.append(
            f"  [{mark}] {stage.phase:<16} {stage.role or '-':<12} {stage.model or '-':<22} "
            f"${stage.cost_usd:.4f} {stage.lifecycle}"
        )
        if stage.detail:
            lines.append(f"         {stage.detail}")
    if report.refusals:
        lines += ["", "REFUSALS"]
        lines += [f"  - {refusal}" for refusal in report.refusals]
    if report.ready_for_human:
        lines += [
            "",
            "HUMAN MERGE GATE — every deterministic gate passed and an independent",
            "reviewer approved. Nothing was merged. The merge is yours to make.",
        ]
    return "\n".join(lines)


__all__ = [
    "Auditor",
    "Dispatch",
    "Dispatcher",
    "DryRunPlan",
    "Lifecycle",
    "Phase",
    "RunSpec",
    "StageRecord",
    "ValidationLevel",
    "Validator",
    "WorkerResult",
    "WorkflowEngine",
    "WorkflowReport",
    "build_review_packet",
    "next_after_review",
    "summarise",
]
