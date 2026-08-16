"""Claiming: every precondition checked before a model is ever launched.

v3 launched the model and hoped. Here a claim is a conjunction of checks, and
any failure is a REFUSED with a machine-readable reason:

* the task is formally ready under the exact project rule;
* it is not already actively claimed elsewhere;
* no same-repository runtime write conflict;
* no same-repository exclusive resource is held by another active task;
* the base commit is known;
* the bounded context compiles;
* the required human PLAN gate, if policy demands one, is satisfied;
* the model budget permits the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import readiness, resources
from .approvals import ApprovalStore, required_gates
from .config import Config, RepoConfig
from .context_compiler import ContextCompiler, ContextManifest
from .errors import OrchestratorError, Reason, Refusal
from .registers import Registers
from .state import State, StateStore
from .task_loader import Task
from .worktrees import WorktreeManager, WorktreeRecord


@dataclass
class ClaimDecision:
    """Whether a task may run a stage right now, and precisely why not."""

    repo: str
    task_id: str
    stage: str
    allowed: bool = False
    base_sha: str = ""
    refusals: list[Refusal] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    manifest: ContextManifest | None = None
    worktree: WorktreeRecord | None = None

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "task_id": self.task_id,
            "stage": self.stage,
            "allowed": self.allowed,
            "base_sha": self.base_sha,
            "resources": self.resources,
            "worktree": self.worktree.as_dict() if self.worktree else None,
            "refusals": [refusal.as_dict() for refusal in self.refusals],
        }


def evaluate_claim(
    config: Config,
    repo: RepoConfig,
    task: Task,
    tasks: dict[str, Task],
    registers: Registers,
    states: StateStore,
    approvals: ApprovalStore,
    base_sha: str,
    stage: str,
    plan_text: str | None = None,
) -> ClaimDecision:
    """Run every claim precondition. Never short-circuits: report them all."""
    decision = ClaimDecision(repo=repo.name, task_id=task.id, stage=stage, base_sha=base_sha)

    # Every model stage runs inside the recorded task worktree. There is no
    # fallback to the role repository checkout: a forgotten `worktree` step must
    # refuse, not quietly run the model against the real Police/Thief tree.
    manager = WorktreeManager(config)
    record = manager.load(repo, task.id)
    if record is None or not Path(record.path).is_dir():
        decision.refusals.append(
            Refusal(
                Reason.WORKTREE_MISSING,
                f"{repo.name}/{task.id}: no task worktree. Create it explicitly first: "
                f"worktree --repo {repo.name} --task {task.id}",
                {"stage": stage},
            )
        )
    else:
        decision.worktree = record
        if record.base_sha != base_sha:
            decision.refusals.append(
                Refusal(
                    Reason.WORKTREE_BASE_MISMATCH,
                    f"{repo.name}/{task.id}: worktree base {record.base_sha[:12]} does not match "
                    f"the base for this stage {base_sha[:12]}",
                    {"worktree_base": record.base_sha, "stage_base": base_sha},
                )
            )
        stale = manager.staleness(repo, record)
        if stale:
            decision.refusals.append(
                Refusal(Reason.WORKTREE_STALE_BASE, f"{repo.name}/{task.id}: {stale}", {"stage": stage})
            )

    verdict = readiness.evaluate(task, tasks, registers)
    decision.refusals += verdict.refusals

    active_states = states.active(repo)
    own = active_states.pop(task.id, None)
    if own is not None and own.claimed_by and stage == "plan" and own.current is not State.READY:
        decision.refusals.append(
            Refusal(
                Reason.ALREADY_CLAIMED,
                f"{task.id} is already active in state {own.state} (claimed by {own.claimed_by})",
                {"state": own.state},
            )
        )

    active_tasks = {
        task_id: tasks[task_id] for task_id in active_states if task_id in tasks
    }
    decision.refusals += readiness.write_conflicts(task, active_tasks)

    wanted = resources.required_resources(repo.name, task, config.resources)
    decision.resources = [str(handle) for handle in wanted]
    holders = states.resource_holders(repo)
    holders = {key: [t for t in ids if t != task.id] for key, ids in holders.items()}
    for handle, current in resources.conflicts(wanted, holders):
        decision.refusals.append(
            Refusal(
                Reason.RESOURCE_HELD,
                f"{task.id}: repository-global resource {handle} is held by {current}; "
                "dependency-artifact mutation must be serialized",
                {"resource": str(handle), "holders": current},
            )
        )

    if not base_sha:
        decision.refusals.append(
            Refusal(Reason.WORKTREE_MISSING, f"{task.id}: no base commit resolved")
        )

    # Context is read from the task worktree, so it describes the tree actually
    # being worked on, while the manifest keeps the original repo identity.
    tree = Path(decision.worktree.path) if decision.worktree else repo.path
    compiler = ContextCompiler(config, repo, Registers(tree, config.project), tree=tree)
    try:
        decision.manifest = compiler.compile(task, base_sha, strict=True)
    except OrchestratorError as exc:
        decision.refusals.append(exc.refusal)

    if stage == "implement" and "plan" in required_gates(task.risk, config.review_policy):
        if plan_text is None:
            decision.refusals.append(
                Refusal(
                    Reason.APPROVAL_MISSING,
                    f"{task.id}: risk={task.risk} requires an approved plan, but no plan "
                    "artifact exists yet",
                    {"stage": "plan"},
                )
            )
        else:
            check = approvals.check(repo, task.id, "plan", base_sha, plan_text)
            if not check.valid and check.refusal is not None:
                decision.refusals.append(check.refusal)

    decision.allowed = not decision.refusals
    return decision
