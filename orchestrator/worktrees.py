"""Repository-qualified task worktrees.

Police T002 and Thief T002 are different tasks in different repositories.
v3's ``.wt-T002`` collided on the task ID alone. Here every worktree lives at
``worktrees/<repo>/<task>/`` and carries a recorded base commit; a worktree
built on a base that is no longer current is reported stale rather than reused.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitio
from .config import Config, RepoConfig
from .errors import OrchestratorError, Reason

AGENT_DIR_NAME = ".agent"


@dataclass(frozen=True)
class WorktreeRecord:
    """Provenance for one task worktree."""

    repo: str
    repo_identity: str
    task_id: str
    base_ref: str
    base_sha: str
    branch: str
    path: str
    created_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class WorktreeManager:
    """Creates, records and validates namespaced task worktrees."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def path_for(self, repo: RepoConfig, task_id: str) -> Path:
        return self.config.worktree_root / repo.name / task_id

    def record_path(self, repo: RepoConfig, task_id: str) -> Path:
        return self.config.state_dir / "worktrees" / repo.name / f"{task_id}.json"

    def agent_dir(self, repo: RepoConfig, task_id: str) -> Path:
        """Runtime metadata lives in the workspace state tree, NOT in the worktree.

        Prompts, plans, logs and receipts therefore cannot be committed as
        product code even by accident.
        """
        directory = self.config.state_dir / "agent" / repo.name / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def branch_for(self, task_id: str) -> str:
        return f"task/{task_id}"

    def load(self, repo: RepoConfig, task_id: str) -> WorktreeRecord | None:
        path = self.record_path(repo, task_id)
        if not path.is_file():
            return None
        return WorktreeRecord(**json.loads(path.read_text(encoding="utf-8")))

    def resolve_base(self, repo: RepoConfig, base_ref: str | None = None) -> tuple[str, str]:
        ref = base_ref or repo.default_base
        return ref, gitio.rev_parse(repo.path, ref)

    def create(
        self, repo: RepoConfig, task_id: str, base_ref: str | None = None, branch: str | None = None
    ) -> WorktreeRecord:
        """Create a worktree pinned to an explicitly resolved base commit."""
        ref, base_sha = self.resolve_base(repo, base_ref)
        target = self.path_for(repo, task_id)
        branch_name = branch or self.branch_for(task_id)
        if target.exists():
            raise OrchestratorError(
                Reason.CONFIG_INVALID, f"worktree already exists: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        gitio.git(["worktree", "add", "-b", branch_name, str(target), base_sha], repo.path)
        record = WorktreeRecord(
            repo=repo.name,
            repo_identity=repo.identity,
            task_id=task_id,
            base_ref=ref,
            base_sha=base_sha,
            branch=branch_name,
            path=str(target),
            created_at=_utc_now(),
        )
        path = self.record_path(repo, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.as_dict(), indent=2), encoding="utf-8")
        return record

    def require(self, repo: RepoConfig, task_id: str) -> WorktreeRecord:
        record = self.load(repo, task_id)
        if record is None or not Path(record.path).is_dir():
            raise OrchestratorError(
                Reason.WORKTREE_MISSING,
                f"no worktree for {repo.name}/{task_id}; create it first",
            )
        return record

    def staleness(self, repo: RepoConfig, record: WorktreeRecord) -> str | None:
        """Report — never auto-fix — a worktree whose base ref has moved on."""
        current = gitio.rev_parse(repo.path, record.base_ref)
        if current != record.base_sha:
            return (
                f"worktree base {record.base_sha[:12]} predates {record.base_ref} "
                f"@ {current[:12]}; rebase or recreate before shipping"
            )
        return None

    def require_fresh(self, repo: RepoConfig, task_id: str) -> WorktreeRecord:
        record = self.require(repo, task_id)
        stale = self.staleness(repo, record)
        if stale:
            raise OrchestratorError(
                Reason.WORKTREE_STALE_BASE, f"{repo.name}/{task_id}: {stale}"
            )
        return record
