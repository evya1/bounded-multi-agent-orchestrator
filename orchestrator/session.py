"""One resolved (config, repo) working context.

Extracted from the CLI so the workflow runner can share it without importing the
command layer. Behaviour is unchanged; ``orchestrator.cli.Session`` still
resolves to this class.
"""

from __future__ import annotations

from pathlib import Path

from . import budget as budget_mod
from . import gitio
from .approvals import ApprovalStore
from .config import Config, RepoConfig
from .model_router import ModelRouter
from .registers import Registers
from .state import StateStore
from .task_loader import Task, get_task, load_tasks
from .worktrees import WorktreeManager


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
