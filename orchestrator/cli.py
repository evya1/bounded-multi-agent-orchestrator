"""Command-line interface.

Every command is explicit about which repository it addresses; there is no
default repo, because Police T002 and Thief T002 are different tasks.

No command spends money without ``--yes-spend``. Without it, a stage that would
invoke a paid model prints the model it *would* use, the estimated cost and the
budget verdict, then stops.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import budget as budget_mod
from . import doctor as doctor_mod
from . import gitio, prompts, readiness, resources, write_guard
from .adapters import build_adapter
from .approvals import ApprovalStore, diff_gate_required, required_gates
from .claiming import evaluate_claim
from .config import Config, RepoConfig, load_config
from .context_compiler import ContextCompiler, render_human
from .errors import OrchestratorError, Reason, Refusal
from .model_router import ModelRouter
from .registers import Registers
from .state import State, StateStore
from .task_loader import Task, get_task, load_tasks
from .verifier import IGNORED_PREFIXES, audit_write_set, verify
from .worktrees import WorktreeManager

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2


class Session:
    """One resolved (config, repo) working context."""

    def __init__(self, config: Config, repo_name: str) -> None:
        self.config = config
        self.repo: RepoConfig = config.repo(repo_name)
        self.registers = Registers(self.repo.path, config.project)
        self.tasks = load_tasks(self.repo.path, config.project.task_dirs)
        self.states = StateStore(config)
        self.approvals = ApprovalStore(config)
        self.router = ModelRouter(config.models)
        self.ledger = budget_mod.Ledger(config)

    def task(self, task_id: str) -> Task:
        return get_task(self.tasks, task_id)

    def base_sha(self, task_id: str) -> str:
        """The task's recorded worktree base, else the repository's default base."""
        record = WorktreeManager(self.config).load(self.repo, task_id)
        if record is not None:
            return record.base_sha
        return gitio.rev_parse(self.repo.path, self.repo.default_base)

    def require_worktree(self, task_id: str, fresh: bool = True):
        """The recorded task worktree, or a refusal.

        There is deliberately NO fallback to the role repository checkout. A
        forgotten `worktree` step must refuse rather than silently run against
        the real Police/Thief tree.
        """
        manager = WorktreeManager(self.config)
        return manager.require_fresh(self.repo, task_id) if fresh else manager.require(self.repo, task_id)

    def workspace(self, task_id: str, fresh: bool = True) -> Path:
        """Where a stage reads and writes: always the recorded task worktree."""
        return Path(self.require_worktree(task_id, fresh).path)

    def agent_dir(self, task_id: str) -> Path:
        return WorktreeManager(self.config).agent_dir(self.repo, task_id)


def _emit(data: object, as_json: bool, human: str) -> None:
    print(json.dumps(data, indent=2) if as_json else human)


def _refuse(refusals: list[Refusal], as_json: bool) -> int:
    if as_json:
        print(json.dumps({"refused": True, "refusals": [r.as_dict() for r in refusals]}, indent=2))
    else:
        print("REFUSED")
        for refusal in refusals:
            print(f"  - {refusal}")
    return EXIT_REFUSED


# --------------------------------------------------------------------- doctor


def cmd_doctor(args, config: Config) -> int:
    checks = doctor_mod.run(config)
    _emit([c.as_dict() for c in checks], args.json, doctor_mod.render(checks))
    return EXIT_OK


# --------------------------------------------------------------- status/queue


def _readiness_rows(session: Session) -> list[dict]:
    rows = []
    for verdict in readiness.ready_tasks(session.tasks, session.registers):
        task = session.tasks[verdict.task_id]
        state = session.states.load(session.repo, task.id)
        rows.append(
            {
                **verdict.as_dict(),
                "declared_status": task.status,
                "risk": task.risk,
                "priority": task.priority,
                "runtime_state": state.state if state else str(State.READY),
                "human_gates": sorted(required_gates(task.risk, session.config.review_policy)),
                "resources": [
                    str(h)
                    for h in resources.required_resources(
                        session.repo.name, task, session.config.resources
                    )
                ],
            }
        )
    return rows


def cmd_status(args, config: Config) -> int:
    payload = []
    for repo_name in args.repos or sorted(config.repos):
        session = Session(config, repo_name)
        rows = _readiness_rows(session)
        payload.append({"repo": repo_name, "tasks": rows})

    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    for entry in payload:
        print(f"=== repo: {entry['repo']} ===")
        print(f"{'TASK':6} {'READY':6} {'DECL':9} {'RISK':7} {'STATE':24} GATES / BLOCKERS")
        for row in entry["tasks"]:
            gates = ", ".join(
                f"{g['id']}({g['blocks']}:{g['state'].replace('UNKNOWN_GATE_STATE', 'UNKNOWN')})"
                for g in row["gates"]
            ) or "-"
            blockers = "; ".join(r["reason"] for r in row["refusals"])
            print(
                f"{row['task_id']:6} {'YES' if row['ready'] else 'no':6} {row['declared_status']:9} "
                f"{row['risk']:7} {row['runtime_state']:24} {gates}"
                + (f"  | {blockers}" if blockers else "")
            )
        print()
    return EXIT_OK


def cmd_queue(args, config: Config) -> int:
    """Only formally-ready tasks, with the next action for each."""
    payload = []
    for repo_name in args.repos or sorted(config.repos):
        session = Session(config, repo_name)
        for row in _readiness_rows(session):
            if not row["ready"]:
                continue
            state = row["runtime_state"]
            next_action = {
                str(State.READY): "plan",
                str(State.PLANNED): "approve --stage plan  (if required) then implement",
                str(State.PLAN_APPROVAL_REQUIRED): "approve --stage plan",
                str(State.IMPLEMENTED): "verify",
                str(State.VERIFIED): "review",
                str(State.REVIEWED): "approve --stage diff",
                str(State.DIFF_APPROVAL_REQUIRED): "approve --stage diff",
                str(State.PR_READY): "pr",
            }.get(state, "inspect")
            payload.append({"repo": repo_name, **row, "next_action": next_action})

    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print(f"{'REPO':8} {'TASK':6} {'RISK':7} {'STATE':24} {'HUMAN GATES':16} NEXT")
    for row in payload:
        print(
            f"{row['repo']:8} {row['task_id']:6} {row['risk']:7} {row['runtime_state']:24} "
            f"{','.join(row['human_gates']) or '-':16} {row['next_action']}"
        )
    return EXIT_OK


# -------------------------------------------------------------------- context


def cmd_context(args, config: Config) -> int:
    """Read-only context inspection.

    Prefers the task worktree. Without one it will only read the main checkout
    after PROVING that checkout is the intended clean tree at the base commit —
    otherwise it refuses rather than presenting mismatched context.
    """
    session = Session(config, args.repo)
    task = session.task(args.task)
    record = WorktreeManager(config).load(session.repo, task.id)

    if record is not None and Path(record.path).is_dir():
        tree, base_sha = Path(record.path), record.base_sha
    else:
        base_sha = gitio.rev_parse(session.repo.path, session.repo.default_base)
        tree = session.repo.path
        refusal = _prove_clean_tree(session.repo.path, base_sha)
        if refusal is not None:
            return _refuse([refusal], args.json)

    compiler = ContextCompiler(config, session.repo, Registers(tree, config.project), tree=tree)
    try:
        manifest = compiler.compile(task, base_sha, strict=not args.allow_missing)
    except OrchestratorError as exc:
        return _refuse([exc.refusal], args.json)

    if args.json:
        print(json.dumps(manifest.as_dict(), indent=2))
    elif args.render_prompt:
        print(manifest.render_prompt_context())
    else:
        print(render_human(manifest))
    return EXIT_OK


def _prove_clean_tree(repo_path: Path, base_sha: str) -> Refusal | None:
    """Is this checkout demonstrably the intended clean tree at ``base_sha``?

    Two conditions, both mechanical: no uncommitted or untracked change, and a
    working tree whose content equals the base commit's tree. A branch pointing
    at a different commit is fine as long as the TREE matches — that is what the
    context actually depends on.
    """
    if not gitio.is_clean(repo_path):
        return Refusal(
            Reason.TREE_NOT_PROVEN,
            f"{repo_path} has uncommitted or untracked changes, so it cannot be proven to be "
            f"the clean tree at {base_sha[:12]}. Create a task worktree instead.",
            {"path": str(repo_path), "base_sha": base_sha},
        )
    head_tree = gitio.git(["rev-parse", "HEAD^{tree}"], repo_path).strip()
    base_tree = gitio.git(["rev-parse", f"{base_sha}^{{tree}}"], repo_path).strip()
    if head_tree != base_tree:
        return Refusal(
            Reason.TREE_NOT_PROVEN,
            f"{repo_path} checkout tree {head_tree[:12]} differs from the base tree "
            f"{base_tree[:12]} at {base_sha[:12]}; refusing to present mismatched context",
            {"head_tree": head_tree, "base_tree": base_tree, "base_sha": base_sha},
        )
    return None


# ------------------------------------------------------------------ worktrees


def cmd_worktree(args, config: Config) -> int:
    session = Session(config, args.repo)
    manager = WorktreeManager(config)
    task = session.task(args.task)
    existing = manager.load(session.repo, task.id)
    if existing is not None:
        stale = manager.staleness(session.repo, existing)
        payload = {**existing.as_dict(), "stale": stale}
        _emit(payload, args.json, json.dumps(payload, indent=2))
        return EXIT_OK
    record = manager.create(session.repo, task.id, args.base)
    _emit(record.as_dict(), args.json, json.dumps(record.as_dict(), indent=2))
    return EXIT_OK


# -------------------------------------------------------------- model stages


def _plan_path(session: Session, task_id: str) -> Path:
    return session.agent_dir(task_id) / f"{task_id}-plan.md"


def _estimate(session: Session, choice, manifest_bytes: int) -> float:
    """Pre-call cost estimate: context bytes as a token proxy, plus configured output."""
    input_tokens = max(1, manifest_bytes // 4)
    return budget_mod.estimate_cost(
        choice.price, input_tokens, session.config.budget.estimate_output_tokens
    )


def _announce(choice, estimated: float, extra_notes: list[str]) -> None:
    print(f"MODEL     {choice.name}  ({choice.label}, family={choice.family})")
    print(f"ROLE      {choice.role}")
    print(f"PAID      {'YES — OpenRouter' if choice.paid else 'no — local/free'}")
    print(f"PRIVILEGE tools={choice.privileges.get('tools')} bash={choice.privileges.get('bash')}")
    print(f"ESTIMATE  ${estimated:.4f} (ESTIMATED from catalog metadata, not an invoice)")
    for note in extra_notes:
        print(f"NOTE      {note}")


def _run_stage(args, config: Config, stage: str) -> int:
    session = Session(config, args.repo)
    task = session.task(args.task)
    base_sha = session.base_sha(task.id)
    plan_text = None
    plan_file = _plan_path(session, task.id)
    if plan_file.is_file():
        plan_text = plan_file.read_text(encoding="utf-8")

    decision = evaluate_claim(
        config,
        session.repo,
        task,
        session.tasks,
        session.registers,
        session.states,
        session.approvals,
        base_sha,
        stage,
        plan_text,
    )
    if not decision.allowed:
        return _refuse(decision.refusals, args.json)

    # evaluate_claim has already proven the worktree exists, matches this base
    # and is fresh; every model stage runs inside it.
    assert decision.worktree is not None
    worktree = Path(decision.worktree.path)

    complexity = args.complexity or task.risk
    choice, refusals, notes = session.router.resolve(stage, complexity, args.model)
    if choice is None:
        return _refuse(refusals, args.json)

    manifest = decision.manifest
    assert manifest is not None
    change_summary = None
    if stage == "review":
        # The reviewer gets the ACTUAL patch and new-file contents. A hash
        # manifest identifies a change; it does not let anyone review code.
        change_summary = gitio.review_patch(worktree, base_sha, IGNORED_PREFIXES)
    prompt = prompts.build_prompt(stage, task, manifest, plan_text, change_summary)
    prompt_file = session.agent_dir(task.id) / f"{task.id}-{stage}-prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    # A stage declaring write_set_only gets a real pre-write path guard.
    policy = None
    if (choice.privileges or {}).get("write_set_only"):
        policy = write_guard.policy_for(worktree, task.write_set)

    estimated = _estimate(session, choice, len(prompt.encode()))
    _announce(choice, estimated, notes)
    print(f"WORKTREE  {worktree} @ {base_sha[:12]}")
    if policy is not None:
        print(f"GUARD     bounded writes to {list(policy.write_set)} inside the worktree")
    print(f"PROMPT    {prompt_file} ({len(prompt.encode())} bytes)")

    adapter = build_adapter(args.adapter, (config.models.get("adapters") or {}).get(args.adapter))
    if args.dry_run:
        # A dry run spends nothing, so it is inspectable without authorizing spend.
        print("DRY RUN   " + " ".join(adapter.build_argv(choice, prompt_file, stage, policy)))
        return EXIT_OK

    refusal = session.ledger.authorize(choice.paid, estimated)
    if refusal is not None:
        return _refuse([refusal], args.json)

    if choice.paid and not args.yes_spend:
        return _refuse(
            [
                Refusal(
                    Reason.SPEND_NOT_AUTHORIZED,
                    f"{choice.label} is a paid model. Re-run with --yes-spend to authorize "
                    f"approximately ${estimated:.4f}.",
                    {"model": choice.name, "estimated_usd": estimated},
                )
            ],
            args.json,
        )

    result = adapter.run(choice, prompt_file, stage, worktree, args.timeout, policy)

    # 1. Cost and usage are recorded whatever the outcome — a failed call still
    #    consumed tokens, and hiding that would corrupt the budget.
    cost, source = (
        (result.reported_cost_usd, budget_mod.CostSource.REPORTED)
        if result.reported_cost_usd is not None
        else (
            budget_mod.estimate_cost(choice.price, result.input_tokens, result.output_tokens),
            budget_mod.CostSource.ESTIMATED,
        )
    )
    if not choice.paid:
        cost, source = 0.0, budget_mod.CostSource.FREE_LOCAL
    now = budget_mod.dt.datetime.now(budget_mod.dt.UTC)
    session.ledger.append(
        budget_mod.LedgerEntry(
            at=now.isoformat(),
            day=now.strftime("%Y-%m-%d"),
            repo=session.repo.name,
            task_id=task.id,
            stage=stage,
            role=choice.role,
            provider=choice.provider,
            model=choice.model_id or choice.name,
            paid=choice.paid,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            cost_usd=float(cost or 0.0),
            cost_source=str(source),
            duration_s=result.duration_s,
            exit_code=result.exit_code,
        )
    )

    # 2. Diagnostic output is always preserved, successful or not.
    output_file = session.agent_dir(task.id) / f"{task.id}-{stage}-output.md"
    output_file.write_text(result.text, encoding="utf-8")

    state = session.states.get_or_create(session.repo, task.id)
    state.base_sha = base_sha
    state.resources = decision.resources
    session.states.save(session.repo, state)

    # 3. An escalation is not progress. Do not advance the stage.
    if result.stopped:
        print(f"\nSTOP_NEEDS_ORCHESTRATOR — the worker escalated instead of guessing. "
              f"State stays {state.state}. Output: {output_file}")
        print(result.text[:4000])
        return _refuse(
            [
                Refusal(
                    Reason.STOP_NEEDS_ORCHESTRATOR,
                    f"{session.repo.name}/{task.id}: {stage} worker escalated; no state change",
                    {"stage": stage, "output": str(output_file)},
                )
            ],
            args.json,
        )

    # 4. A failed model call is not progress either. No transition, no
    #    provenance: a non-zero exit must never leave a task looking done.
    if result.exit_code != 0:
        print(f"\nMODEL CALL FAILED (exit {result.exit_code}). State stays {state.state}; "
              f"no provenance recorded. Output: {output_file}")
        print(result.text[-2000:])
        return EXIT_FAIL

    # 5. Only a successful result advances the state machine.
    if stage == "plan":
        plan_file.write_text(result.text, encoding="utf-8")
        session.states.transition(session.repo, state, State.PLANNED, "plan produced")
        if "plan" in required_gates(task.risk, config.review_policy):
            session.states.transition(
                session.repo, state, State.PLAN_APPROVAL_REQUIRED, "policy requires plan approval"
            )
        print(f"\nplan saved: {plan_file}\nREAD IT, then: approve --repo {session.repo.name} "
              f"--task {task.id} --stage plan")
    elif stage == "implement":
        session.states.transition(session.repo, state, State.IMPLEMENTED, f"implemented by {choice.name}")
        state.provenance["implemented_by"] = choice.model_id or choice.name
        session.states.save(session.repo, state)
    elif stage == "review":
        session.states.transition(session.repo, state, State.REVIEWED, f"reviewed by {choice.name}")
        state.provenance["reviewed_by"] = choice.model_id or choice.name
        state.provenance["review_verdict"] = "APPROVE" if "APPROVE" in result.text[-200:] else "REJECT"
        session.states.save(session.repo, state)
        print(result.text[-4000:])
    return EXIT_OK


def cmd_plan(args, config: Config) -> int:
    return _run_stage(args, config, "plan")


def cmd_implement(args, config: Config) -> int:
    return _run_stage(args, config, "implement")


def cmd_review(args, config: Config) -> int:
    return _run_stage(args, config, "review")


# --------------------------------------------------------------------- verify


def cmd_verify(args, config: Config) -> int:
    session = Session(config, args.repo)
    task = session.task(args.task)
    record = WorktreeManager(config).require_fresh(session.repo, task.id)
    report = verify(
        session.repo.name,
        task.id,
        Path(record.path),
        record.base_sha,
        task.write_set,
        task.verification_commands,
        args.timeout,
    )
    report_file = session.agent_dir(task.id) / f"{task.id}-verify.json"
    report_file.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(f"VERIFY {session.repo.name}/{task.id} @ {record.base_sha[:12]}")
        for result in report.commands:
            print(f"  [{'PASS' if result.passed else 'FAIL'}] {result.command}")
            if not result.passed:
                print(f"        {result.output_tail[-600:]}")
        for refusal in report.write_set_refusals + report.side_effects:
            print(f"  [VIOL] {refusal}")
        print(f"  => {'PASS' if report.passed else 'FAIL'}")

    if report.passed:
        state = session.states.get_or_create(session.repo, task.id)
        state.base_sha = record.base_sha
        session.states.transition(session.repo, state, State.VERIFIED, "deterministic verification passed")
        return EXIT_OK
    return EXIT_FAIL


# ------------------------------------------------------------------ approvals


def _artifact_for(session: Session, task: Task, stage: str, base_sha: str) -> tuple[str | None, str]:
    """The exact artifact bytes an approval binds to, and its description."""
    if stage == "plan":
        path = _plan_path(session, task.id)
        if not path.is_file():
            return None, f"no plan artifact at {path}"
        return path.read_text(encoding="utf-8"), str(path)
    workspace = session.workspace(task.id)  # refuses if no fresh worktree exists
    return (
        gitio.change_manifest(workspace, base_sha, IGNORED_PREFIXES),
        f"canonical change manifest (path/mode/content) of {workspace} vs {base_sha[:12]}",
    )


def cmd_approve(args, config: Config) -> int:
    session = Session(config, args.repo)
    task = session.task(args.task)
    base_sha = session.base_sha(task.id)
    artifact, description = _artifact_for(session, task, args.stage, base_sha)
    if artifact is None:
        return _refuse(
            [Refusal(Reason.APPROVAL_MISSING, f"nothing to approve: {description}")], args.json
        )
    approved = args.command == "approve"
    approval = session.approvals.record(
        session.repo, task.id, args.stage, base_sha, artifact, approved, args.note
    )
    _emit(
        approval.as_dict(),
        args.json,
        f"{'APPROVED' if approved else 'REJECTED'} {session.repo.name}/{task.id} stage={args.stage}\n"
        f"  artifact:  {description}\n"
        f"  sha256:    {approval.artifact_sha256}\n"
        f"  base:      {base_sha}\n"
        f"  identity:  {session.repo.identity}\n"
        f"  at:        {approval.at}",
    )
    if approved and args.stage == "diff":
        state = session.states.get_or_create(session.repo, task.id)
        if state.current in (State.REVIEWED, State.VERIFIED):
            session.states.transition(
                session.repo, state, State.DIFF_APPROVAL_REQUIRED, "human diff approval recorded"
            )
    return EXIT_OK


# ------------------------------------------------------------------------- pr


def cmd_pr(args, config: Config) -> int:
    """Commit candidate, push task branch, report. Never merges."""
    session = Session(config, args.repo)
    task = session.task(args.task)
    record = WorktreeManager(config).require_fresh(session.repo, task.id)
    workspace = Path(record.path)
    state = session.states.get_or_create(session.repo, task.id)
    refusals: list[Refusal] = []

    if state.current not in (State.VERIFIED, State.REVIEWED, State.DIFF_APPROVAL_REQUIRED):
        refusals.append(
            Refusal(
                Reason.VERIFICATION_NOT_RUN,
                f"{task.id} is in state {state.state}; deterministic verification must pass "
                "before shipping",
                {"state": state.state},
            )
        )

    _, write_refusals = audit_write_set(workspace, record.base_sha, task.write_set)
    refusals += write_refusals

    # Post-start project gates are PENDING STATUS, never a blocker for a local
    # candidate PR. `blocks: criterion` stops the named criterion from being
    # claimed satisfied and the task from being marked done — it does not stop
    # implementation, verification, review, or PR_READY. `blocks: integration`
    # is the same: local work and a candidate PR may proceed; only a claim of
    # PASSED integration is illegitimate, and v4 has no such claim to make yet.
    task_registers = Registers(workspace, config.project)
    pending = readiness.pending_project_gates(task, task_registers)

    changed = gitio.changed_paths(workspace, record.base_sha, IGNORED_PREFIXES)
    needed, why = diff_gate_required(
        task.risk, changed, config.review_policy, config.governance_paths
    )
    artifact = gitio.change_manifest(workspace, record.base_sha, IGNORED_PREFIXES)
    diff_check = session.approvals.check(session.repo, task.id, "diff", record.base_sha, artifact)
    if needed and not diff_check.valid and diff_check.refusal is not None:
        refusals.append(
            Refusal(diff_check.refusal.reason, f"{why}; {diff_check.refusal.message}", diff_check.refusal.detail)
        )
    if refusals:
        return _refuse(refusals, args.json)

    if any(pending.values()) and not args.json:
        print("PENDING PROJECT GATES (do not block this candidate PR; PR_READY != DONE):")
        for line in readiness.format_pending(pending):
            print(f"  - {line}")
        print("  This criterion is not satisfied and this task is not done while these remain.")

    message = prompts.commit_message(
        task,
        state.provenance.get("implemented_by"),
        state.provenance.get("reviewed_by"),
        session.approvals.load(session.repo, task.id, "plan").artifact_sha256
        if session.approvals.load(session.repo, task.id, "plan")
        else None,
        diff_check.approval.artifact_sha256 if diff_check.approval else None,
        bool(diff_check.valid),
    )
    print(message)
    if args.dry_run:
        print(f"DRY RUN — would commit {len(changed)} path(s) on {record.branch} and push.")
        return EXIT_OK

    gitio.git(["add", "--", *task.write_set], workspace)
    gitio.git(["commit", "-m", message], workspace)
    gitio.git(["push", "-u", "origin", record.branch], workspace)
    state.provenance["pending_project_gates"] = pending
    session.states.save(session.repo, state)
    session.states.transition(session.repo, state, State.PR_READY, "branch pushed")
    print(f"pushed {record.branch}. PR_READY means eligible to commit/push/open a PR — "
          "NOT task done, NOT all criteria satisfied, NOT integration passed. "
          "Master merge remains a separate human action.")
    return EXIT_OK


# --------------------------------------------------------------------- budget


def cmd_budget(args, config: Config) -> int:
    ledger = budget_mod.Ledger(config)
    status = ledger.status(args.day)
    _emit(status.as_dict(), args.json, budget_mod.render_status(status))
    return EXIT_OK


def cmd_models(args, config: Config) -> int:
    router = ModelRouter(config.models)
    payload = {
        "models": [router.availability(name).as_dict() for name in sorted(router.models)],
        "routing": {
            complexity: {
                stage: router.roles.get(role, {}).get("primary")
                for stage, role in stages.items()
            }
            for complexity, stages in router.escalation.items()
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    print("MODEL AVAILABILITY")
    for row in payload["models"]:
        print(f"  [{'ok  ' if row['available'] else 'FAIL'}] {row['model']:<20} {row['detail']}")
    print("\nROUTING (complexity -> stage -> model)")
    for complexity, stages in payload["routing"].items():
        print(f"  {complexity}: " + ", ".join(f"{s}={m}" for s, m in stages.items()))
    return EXIT_OK


# ------------------------------------------------------------------ arg parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestrate_v4", description="Bounded multi-agent orchestrator (deterministic control plane)."
    )
    parser.add_argument("--config", type=Path, help="path to orchestrator.yaml")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def repo_task(sub, task_required: bool = True):
        sub.add_argument("--repo", required=True)
        sub.add_argument("--task", required=task_required)
        return sub

    subparsers.add_parser("doctor").set_defaults(func=cmd_doctor)

    for name, func in (("status", cmd_status), ("queue", cmd_queue)):
        sub = subparsers.add_parser(name)
        sub.add_argument("--repos", nargs="*", help="limit to these repositories")
        sub.set_defaults(func=func)

    sub = repo_task(subparsers.add_parser("context"))
    sub.add_argument("--allow-missing", action="store_true", help="report rather than fail on gaps")
    sub.add_argument("--render-prompt", action="store_true", help="print the literal context block")
    sub.set_defaults(func=cmd_context)

    sub = repo_task(subparsers.add_parser("worktree"))
    sub.add_argument("--base", help="base ref (default: the repo's configured default_base)")
    sub.set_defaults(func=cmd_worktree)

    for name, func in (("plan", cmd_plan), ("implement", cmd_implement), ("review", cmd_review)):
        sub = repo_task(subparsers.add_parser(name))
        sub.add_argument("--complexity", choices=["low", "medium", "high", "very_high"])
        sub.add_argument("--model", help="manual model override from the routing table")
        sub.add_argument("--adapter", default="pi", choices=["pi", "claude", "fake"])
        sub.add_argument("--yes-spend", action="store_true", help="authorize a paid model call")
        sub.add_argument("--dry-run", action="store_true", help="print argv, invoke nothing")
        sub.add_argument("--timeout", type=int, default=3600)
        sub.set_defaults(func=func)

    sub = repo_task(subparsers.add_parser("verify"))
    sub.add_argument("--timeout", type=int, default=900)
    sub.set_defaults(func=cmd_verify)

    for name in ("approve", "reject"):
        sub = repo_task(subparsers.add_parser(name))
        sub.add_argument("--stage", required=True, choices=["plan", "diff"])
        sub.add_argument("--note", default="")
        sub.set_defaults(func=cmd_approve)

    sub = repo_task(subparsers.add_parser("pr"))
    sub.add_argument("--dry-run", action="store_true")
    sub.set_defaults(func=cmd_pr)

    sub = subparsers.add_parser("budget")
    sub.add_argument("--day", help="UTC date YYYY-MM-DD (default: today)")
    sub.set_defaults(func=cmd_budget)

    subparsers.add_parser("models").set_defaults(func=cmd_models)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        return args.func(args, config)
    except OrchestratorError as exc:
        return _refuse([exc.refusal], getattr(args, "json", False))


if __name__ == "__main__":
    sys.exit(main())
