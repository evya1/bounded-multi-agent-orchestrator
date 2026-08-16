"""Human approvals, cryptographically bound to the exact artifact approved.

v3 stored ``{"approved": true}``. That receipt approves nothing in particular:
the plan can be rewritten, the diff can grow a file, the base can move, and the
boolean still reads true.

Here a receipt names the artifact by SHA-256 and the work by repository
identity, task ID, stage and base commit. Validation recomputes all four. Any
drift invalidates the approval and shipping refuses.

Receipts live in the workspace state tree, outside every task worktree, so no
model worker editing its write_set can create or alter one.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config, RepoConfig
from .errors import Reason, Refusal
from .gitio import sha256_text

STAGES = ("plan", "diff")


@dataclass(frozen=True)
class Approval:
    """One human approval receipt."""

    task_id: str
    repo: str
    repo_identity: str
    stage: str
    base_sha: str
    artifact_sha256: str
    approved: bool
    at: str
    note: str = ""
    approver: str = "human"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ApprovalCheck:
    """The result of validating a receipt against the CURRENT artifact."""

    valid: bool
    refusal: Refusal | None = None
    approval: Approval | None = None


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class ApprovalStore:
    """Reads and writes hash-bound approval receipts."""

    def __init__(self, config: Config) -> None:
        self.root = config.state_dir / "approvals"

    def path_for(self, repo: RepoConfig, task_id: str, stage: str) -> Path:
        return self.root / repo.name / task_id / f"{stage}.json"

    def record(
        self,
        repo: RepoConfig,
        task_id: str,
        stage: str,
        base_sha: str,
        artifact: str,
        approved: bool,
        note: str = "",
        approver: str = "human",
    ) -> Approval:
        """Bind an approval (or rejection) to the artifact's exact bytes."""
        approval = Approval(
            task_id=task_id,
            repo=repo.name,
            repo_identity=repo.identity,
            stage=stage,
            base_sha=base_sha,
            artifact_sha256=sha256_text(artifact),
            approved=approved,
            at=_utc_now(),
            note=note,
            approver=approver,
        )
        path = self.path_for(repo, task_id, stage)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(approval.as_dict(), indent=2), encoding="utf-8")
        return approval

    def load(self, repo: RepoConfig, task_id: str, stage: str) -> Approval | None:
        path = self.path_for(repo, task_id, stage)
        if not path.is_file():
            return None
        return Approval(**json.loads(path.read_text(encoding="utf-8")))

    def check(
        self, repo: RepoConfig, task_id: str, stage: str, base_sha: str, artifact: str
    ) -> ApprovalCheck:
        """Is there a valid human approval for THIS artifact, right now?"""
        approval = self.load(repo, task_id, stage)
        if approval is None:
            return ApprovalCheck(
                False,
                Refusal(
                    Reason.APPROVAL_MISSING,
                    f"{repo.name}/{task_id}: no {stage} approval receipt",
                    {"stage": stage},
                ),
            )
        checks: list[tuple[bool, Reason, str]] = [
            (
                approval.repo_identity == repo.identity,
                Reason.APPROVAL_REPO_MISMATCH,
                f"receipt is for repository {approval.repo_identity!r}, not {repo.identity!r}",
            ),
            (
                approval.task_id == task_id,
                Reason.APPROVAL_TASK_MISMATCH,
                f"receipt is for task {approval.task_id!r}, not {task_id!r}",
            ),
            (
                approval.stage == stage,
                Reason.APPROVAL_STAGE_MISMATCH,
                f"receipt is for stage {approval.stage!r}, not {stage!r}",
            ),
            (
                approval.base_sha == base_sha,
                Reason.APPROVAL_BASE_MISMATCH,
                f"receipt was made against base {approval.base_sha[:12]}, now {base_sha[:12]}",
            ),
            (
                approval.approved,
                Reason.APPROVAL_REJECTED,
                f"the {stage} artifact was explicitly REJECTED: {approval.note or '(no note)'}",
            ),
            (
                approval.artifact_sha256 == sha256_text(artifact),
                Reason.APPROVAL_ARTIFACT_MISMATCH,
                f"the {stage} artifact changed since approval "
                f"(approved {approval.artifact_sha256[:12]}, current {sha256_text(artifact)[:12]})",
            ),
        ]
        for ok, reason, message in checks:
            if not ok:
                return ApprovalCheck(
                    False,
                    Refusal(reason, f"{repo.name}/{task_id}: {message}", {"stage": stage}),
                    approval,
                )
        return ApprovalCheck(True, None, approval)


def required_gates(risk: str, review_policy: dict[str, tuple[str, ...]]) -> set[str]:
    """Which ORCHESTRATION review gates a risk level demands.

    An unknown risk level gets the strictest policy, not the weakest.
    """
    return set(review_policy.get(risk, ("plan", "diff")))


def touches_governance(paths: list[str], governance_paths: tuple[str, ...]) -> list[str]:
    """Changed paths that fall inside governance territory."""
    hits = []
    for path in paths:
        for governed in governance_paths:
            if path == governed or path.startswith(governed if governed.endswith("/") else governed + "/"):
                hits.append(path)
                break
    return sorted(set(hits))


def diff_gate_required(
    risk: str,
    changed: list[str],
    review_policy: dict[str, tuple[str, ...]],
    governance_paths: tuple[str, ...],
) -> tuple[bool, str]:
    """Is a human diff approval mandatory? Governance always wins over risk."""
    governance = touches_governance(changed, governance_paths)
    if governance:
        return True, f"diff touches governance paths {governance}"
    if "diff" in required_gates(risk, review_policy):
        return True, f"review policy for risk={risk} requires a human diff gate"
    return False, f"review policy for risk={risk} requires no human diff gate"
