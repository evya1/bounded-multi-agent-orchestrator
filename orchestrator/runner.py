"""The near-one-command run path.

    uv run orchestrate_v4.py run --config config/runs/<task>.yaml [--dry-run]

Assembles the pieces the workflow needs — context packet, routing, budgets, run
store, dispatcher, validators — and then gets out of the way. The sequencing
decisions all live in ``workflow``; this module is wiring.

Two entry points, and the difference between them is the whole safety story:

``plan``      resolves everything and spends nothing. No paid call, no file
              change, no commit. Safe to run at any time, including against a
              task you are not sure about.
``execute``   the same resolution, then the real bounded workers.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import budget as budget_mod
from . import gitio, prompts, review_packet
from .approvals import required_gates
from .config import Config
from .context_compiler import ContextCompiler, ContextManifest
from .dispatch import PiDispatcher, should_dispatch
from .errors import Reason, Refusal
from .registers import Registers
from .run_store import RunStore, new_task_run_id
from .session import Session
from .verifier import IGNORED_PREFIXES, audit_write_set
from .workflow import (
    Dispatch,
    DryRunPlan,
    Phase,
    RunSpec,
    StageRecord,
    ValidationLevel,
    WorkflowEngine,
    WorkflowReport,
    next_after_review,
)
from .write_guard import policy_for

#: Stages a run resolves, in the order the workflow reaches them.
PLANNED_STAGES = ("implement", "fix", "review", "resolve")


@dataclass
class RunContext:
    """Everything one run needs, resolved once."""

    session: Session
    spec: RunSpec
    task: object
    worktree: Path
    base_sha: str
    manifest: ContextManifest
    complexity: str
    task_run_id: str
    run_store: RunStore
    run_budget: budget_mod.RunBudget


def _run_budget(session: Session, task_run_id: str) -> budget_mod.RunBudget:
    """Assemble the three budget scopes from the routing table."""
    totals = session.config.models.get("run_budget") or {}
    budget = budget_mod.RunBudget(
        session.ledger,
        task_run_id,
        budget_mod.Envelope(
            "run",
            float(totals.get("soft_usd", 0.0) or 0.0),
            float(totals.get("hard_usd", 0.0) or 0.0),
        ),
    )
    for role, envelope in budget_mod.envelopes_from_roles(session.router.roles).items():
        budget.set_role(role, envelope)
    return budget


def prepare(config: Config, spec: RunSpec, task_run_id: str | None = None) -> RunContext:
    """Resolve a run without dispatching anything."""
    session = Session(config, spec.repo)
    task = session.task(spec.task)
    record = session.require_worktree(task.id)
    worktree = Path(record.path)
    base_sha = record.base_sha
    compiler = ContextCompiler(
        config, session.repo, Registers(worktree, config.project), tree=worktree
    )
    manifest = compiler.compile(task, base_sha, strict=False)
    run_id = task_run_id or new_task_run_id(spec.repo, task.id, "run")
    return RunContext(
        session=session,
        spec=spec,
        task=task,
        worktree=worktree,
        base_sha=base_sha,
        manifest=manifest,
        complexity=spec.complexity or task.risk,
        task_run_id=run_id,
        run_store=RunStore(config.state_dir / "runs"),
        run_budget=_run_budget(session, run_id),
    )


# ----------------------------------------------------------------- dry run


def plan(config: Config, spec: RunSpec) -> DryRunPlan:
    """What a real run WOULD do. Calls no model and changes nothing."""
    context = prepare(config, spec)
    router = context.session.router
    report = DryRunPlan(
        repo=spec.repo,
        task_id=context.task.id,
        complexity=context.complexity,
        write_set=list(context.task.write_set),
    )

    for stage in PLANNED_STAGES:
        role = router.role_for(stage, context.complexity)
        if role is None:
            report.routing[stage] = {"role": None, "model": None, "presence": "NOT CONFIGURED"}
            if stage in ("implement", "review"):
                report.refusals.append(
                    Refusal(
                        Reason.CONFIG_INVALID,
                        f"stage {stage!r} has no role at complexity {context.complexity!r}; "
                        "an unconfigured stage fails closed",
                    )
                )
            continue
        choice, refusals, notes = router.resolve(stage, context.complexity)
        availability = router.availability(
            str((router.roles.get(role) or {}).get("primary", ""))
        )
        report.routing[stage] = {
            "role": role,
            "model": availability.name,
            "presence": availability.presence,
            "paid": router.is_paid(role),
            "limits": router.limits_for(role).as_dict(),
        }
        report.availability[availability.name] = availability.as_dict()
        report.notes += notes
        # Only implement and review are required for a green run; fix and
        # resolve are conditional, so an unavailable one is a note, not a block.
        if choice is None and stage in ("implement", "review"):
            report.refusals += refusals

    report.budgets = context.run_budget.as_dict()
    report.context = {
        "files": len(context.manifest.included),
        "bytes": context.manifest.total_bytes,
        "estimated_tokens": context.manifest.total_bytes // 4,
        "refs": context.manifest.included_refs,
    }
    report.validation_plan = {
        str(level): list(
            spec.commands_for(level, tuple(context.task.verification_commands))
            if level != ValidationLevel.LEVEL3
            else spec.commands_for(level)
        )
        for level in ValidationLevel
    }
    report.human_gates = sorted(required_gates(context.task.risk, config.review_policy))
    report.human_gates.append("merge (always — this orchestrator never merges)")
    report.notes.append(
        f"run store: {context.run_store.dir_for(context.task_run_id)} (created on first dispatch)"
    )
    report.notes.append(
        "A dry run resolves routing and budgets only. It does not prove the workers "
        "would succeed, and it does not reserve any budget."
    )
    return report


# ----------------------------------------------------------------- execute


def _run_commands(commands: list[str], cwd: Path, timeout: int = 900) -> tuple[bool, str]:
    """Run one validation level. Exit codes decide; nothing else does."""
    if not commands:
        return True, "(no commands declared for this level)"
    parts: list[str] = []
    passed = True
    for command in commands:
        started = time.monotonic()
        proc = subprocess.run(
            command, cwd=str(cwd), shell=True, text=True, capture_output=True,
            check=False, timeout=timeout,
        )
        ok = proc.returncode == 0
        passed = passed and ok
        parts.append(
            f"$ {command}\n[exit {proc.returncode} in {time.monotonic() - started:.1f}s]\n"
            + (proc.stdout + proc.stderr)[-4000:]
        )
    return passed, "\n\n".join(parts)


def execute(
    config: Config,
    spec: RunSpec,
    *,
    yes_spend: bool = False,
    on_stage=None,
) -> WorkflowReport:
    """The real bounded run. Stops at the human merge gate; never merges."""
    context = prepare(config, spec)
    session = context.session
    task = context.task

    allowed, why = should_dispatch(context.run_store, context.task_run_id)
    if not allowed:
        report = WorkflowReport(spec.repo, task.id, context.task_run_id)
        report.refuse(
            Refusal(
                Reason.STOP_NEEDS_ORCHESTRATOR,
                f"not dispatching: {why}",
                {"task_run_id": context.task_run_id},
            )
        )
        return report

    policy = policy_for(context.worktree, task.write_set)
    dispatcher = PiDispatcher(
        run_store=context.run_store,
        ledger=session.ledger,
        workspace=context.worktree,
        repo_name=spec.repo,
        task_id=task.id,
        task_run_id=context.task_run_id,
        base_sha=context.base_sha,
        executable=str(
            ((config.models.get("adapters") or {}).get("pi") or {}).get("executable", "pi")
        ),
        provider_extensions={
            name: str(provider["pi_extension"])
            for name, provider in (config.models.get("providers") or {}).items()
            if provider.get("pi_extension")
        },
        policy=policy,
    )

    def validator(level: ValidationLevel) -> tuple[bool, str]:
        commands = list(
            spec.commands_for(level, tuple(task.verification_commands))
            if level != ValidationLevel.LEVEL3
            else spec.commands_for(level)
        )
        return _run_commands(commands, context.worktree)

    def auditor() -> tuple[bool, str]:
        _, refusals = audit_write_set(context.worktree, context.base_sha, task.write_set)
        if refusals:
            return False, "\n".join(str(refusal) for refusal in refusals)
        changed = gitio.changed_paths(context.worktree, context.base_sha, IGNORED_PREFIXES)
        return True, f"{len(changed)} path(s) changed, all inside the declared write set"

    engine = WorkflowEngine(
        repo_name=spec.repo,
        task=task,
        task_run_id=context.task_run_id,
        router=session.router,
        run_budget=context.run_budget,
        dispatcher=dispatcher,
        validator=validator,
        auditor=auditor,
        complexity=context.complexity,
        on_stage=on_stage,
    )
    return drive(engine, context, yes_spend=yes_spend)


def drive(
    engine: WorkflowEngine, context: RunContext, *, yes_spend: bool = False
) -> WorkflowReport:
    """The stage sequence itself. Separated so tests can drive a fake engine."""
    report = engine.report
    task = context.task

    report.phase = str(Phase.PREFLIGHT)
    refusals = engine.preflight()
    if refusals:
        for refusal in refusals:
            report.refuse(refusal)
        return report

    if not yes_spend and any(
        engine.router.is_paid(str(engine.router.role_for(stage, engine.complexity)))
        for stage in ("implement", "review")
        if engine.router.role_for(stage, engine.complexity)
    ):
        report.refuse(
            Refusal(
                Reason.SPEND_NOT_AUTHORIZED,
                "this run routes at least one stage to a paid model. Re-run with --yes-spend "
                "to authorize it, or use --dry-run to see exactly what it would cost.",
            )
        )
        return report

    # -- writer ------------------------------------------------------------
    report.phase = str(Phase.WRITER)
    prompt = prompts.build_prompt("implement", task, context.manifest)
    stage, outcome = engine._run_worker(Phase.WRITER, "implement", prompt)
    if outcome is None or not stage.passed:
        return report
    parse, _ = engine.parse_result(outcome)
    context.run_store.write_artifact(
        context.task_run_id, "worker-result.json", parse.as_dict()
    )

    # -- write-set audit (Git decides, not the model) ----------------------
    report.phase = str(Phase.WRITE_SET_AUDIT)
    inside, audit_detail = engine.auditor()
    report.record(
        StageRecord(
            phase=str(Phase.WRITE_SET_AUDIT), passed=inside, detail=audit_detail,
            failure="NONE" if inside else "WRITE_SET_VIOLATION",
        )
    )
    if not inside:
        report.refuse(
            Refusal(
                Reason.OUT_OF_WRITE_SET,
                f"changes fell outside the declared write set; review is not reached:\n{audit_detail}",
            )
        )
        return report

    # -- level 1, then at most ONE fix cycle -------------------------------
    report.phase = str(Phase.LEVEL1)
    passed, output = engine.validator(ValidationLevel.LEVEL1)
    report.record(
        StageRecord(phase=str(Phase.LEVEL1), passed=passed, detail=output[-2000:])
    )
    if not passed:
        report.phase = str(Phase.FIX)
        report.fixer_cycles += 1
        fix_prompt = prompts.build_prompt(
            "fix", task, context.manifest, failure_output=output[-8000:]
        )
        fix_stage, fix_outcome = engine._run_worker(Phase.FIX, "fix", fix_prompt)
        if fix_outcome is None or not fix_stage.passed:
            return report
        passed, output = engine.validator(ValidationLevel.LEVEL1)
        report.record(
            StageRecord(phase=str(Phase.LEVEL1), passed=passed, detail=output[-2000:])
        )
        if not passed:
            report.refuse(
                Refusal(
                    Reason.VERIFICATION_FAILED,
                    "level 1 still fails after the one permitted correction cycle. "
                    "A second automatic cycle is not taken; this needs a human.",
                )
            )
            return report

    # -- level 2, before anything expensive --------------------------------
    report.phase = str(Phase.LEVEL2)
    passed, level2_output = engine.validator(ValidationLevel.LEVEL2)
    report.record(
        StageRecord(phase=str(Phase.LEVEL2), passed=passed, detail=level2_output[-2000:])
    )
    if not passed:
        report.refuse(
            Refusal(
                Reason.VERIFICATION_FAILED,
                "level 2 failed; an expensive review is not spent on a change that does "
                "not pass its own component gates.",
            )
        )
        return report

    # -- review packet, then ONE bounded reviewer --------------------------
    report.phase = str(Phase.REVIEW_PACKET)
    candidate_sha = gitio.rev_parse(context.worktree, "HEAD")
    packet = review_packet.build(
        repo=context.spec.repo,
        task=task,
        worktree=context.worktree,
        base_sha=context.base_sha,
        candidate_sha=candidate_sha,
        acceptance_criteria=context.manifest.render_prompt_context()[:20000],
        validation_output=level2_output,
        write_set_result=audit_detail,
    )
    context.run_store.write_artifact(context.task_run_id, "review-packet.json", packet.as_dict())
    report.record(
        StageRecord(
            phase=str(Phase.REVIEW_PACKET),
            passed=True,
            detail=f"{packet.total_bytes} bytes across {len(packet.sections)} sections",
            artifacts=["review-packet.json"],
        )
    )

    report.phase = str(Phase.REVIEW)
    review_stage, review_outcome = engine._run_worker(Phase.REVIEW, "review", packet.render())
    if review_outcome is None or not review_stage.passed:
        return report
    review_parse, _ = engine.parse_result(review_outcome, reviewer=True)
    context.run_store.write_artifact(
        context.task_run_id, "reviewer-result.json", review_parse.as_dict()
    )
    if not review_parse.ok or review_parse.result is None:
        report.refuse(
            Refusal(
                Reason.STOP_NEEDS_ORCHESTRATOR,
                f"the reviewer's result block could not be validated: {review_parse.error}",
            )
        )
        return report

    next_phase, failure, why = next_after_review(
        review_parse.result, report.fixer_cycles, report.resolver_cycles
    )
    report.record(
        StageRecord(
            phase=str(Phase.REVIEW),
            passed=next_phase == Phase.LEVEL3,
            failure=str(failure),
            detail=why,
        )
    )
    if next_phase == Phase.BLOCKED:
        report.refuse(Refusal(Reason.STOP_NEEDS_ORCHESTRATOR, why))
        return report
    if next_phase != Phase.LEVEL3:
        # A conditional stage is entered at most once, and the run then stops
        # for a human rather than looping back into another review.
        report.phase = str(next_phase)
        if next_phase == Phase.RESOLVE:
            report.resolver_cycles += 1
        else:
            report.fixer_cycles += 1
        report.refuse(
            Refusal(
                Reason.STOP_NEEDS_ORCHESTRATOR,
                f"{why}. The run stops here for a human rather than starting another "
                "writer/reviewer round automatically.",
            )
        )
        return report

    # -- level 3, once, near integration -----------------------------------
    report.phase = str(Phase.LEVEL3)
    passed, output = engine.validator(ValidationLevel.LEVEL3)
    report.record(
        StageRecord(phase=str(Phase.LEVEL3), passed=passed, detail=output[-2000:])
    )
    context.run_store.write_artifact(
        context.task_run_id,
        "validation.json",
        {"level3_passed": passed, "output_tail": output[-8000:]},
    )
    if not passed:
        report.refuse(
            Refusal(Reason.VERIFICATION_FAILED, "level 3 integration validation failed")
        )
        return report

    report.phase = str(Phase.HUMAN_MERGE_GATE)
    report.ready_for_human = True
    report.human_gate = "human merge required — this orchestrator has no merge state"
    return report


__all__ = ["Dispatch", "RunContext", "drive", "execute", "plan", "prepare"]
