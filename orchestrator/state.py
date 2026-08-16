"""The task state machine.

Stages are not arbitrary commands. A task occupies exactly one state, and only
declared transitions are legal, so 'ship' cannot be reached by skipping
verification and 'implement' cannot be reached by skipping a required plan gate.

There is deliberately no autonomous merge state. The terminal state is
``PR_READY``: commit candidate, push task branch, open/update PR, stop.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from .config import Config, RepoConfig
from .errors import OrchestratorError, Reason


class State(StrEnum):
    READY = "READY"
    PLANNED = "PLANNED"
    PLAN_APPROVAL_REQUIRED = "PLAN_APPROVAL_REQUIRED"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"
    REVIEWED = "REVIEWED"
    DIFF_APPROVAL_REQUIRED = "DIFF_APPROVAL_REQUIRED"
    PR_READY = "PR_READY"
    BLOCKED = "BLOCKED"


#: Legal transitions. Rejection and failure paths go backwards to the stage that
#: must be redone, never forwards.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.READY: frozenset({State.PLANNED, State.BLOCKED}),
    State.PLANNED: frozenset(
        {State.PLAN_APPROVAL_REQUIRED, State.IMPLEMENTED, State.PLANNED, State.BLOCKED}
    ),
    State.PLAN_APPROVAL_REQUIRED: frozenset({State.IMPLEMENTED, State.PLANNED, State.BLOCKED}),
    State.IMPLEMENTED: frozenset({State.VERIFIED, State.IMPLEMENTED, State.PLANNED, State.BLOCKED}),
    State.VERIFIED: frozenset(
        {State.REVIEWED, State.DIFF_APPROVAL_REQUIRED, State.IMPLEMENTED, State.BLOCKED}
    ),
    State.REVIEWED: frozenset(
        {State.DIFF_APPROVAL_REQUIRED, State.PR_READY, State.IMPLEMENTED, State.BLOCKED}
    ),
    State.DIFF_APPROVAL_REQUIRED: frozenset({State.PR_READY, State.IMPLEMENTED, State.BLOCKED}),
    State.PR_READY: frozenset({State.IMPLEMENTED, State.BLOCKED}),
    State.BLOCKED: frozenset({State.READY, State.PLANNED, State.IMPLEMENTED}),
}


def can_transition(current: State, target: State) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


@dataclass
class TaskState:
    """Persisted runtime state for one (repo, task) pair."""

    repo: str
    repo_identity: str
    task_id: str
    state: str = str(State.READY)
    base_sha: str = ""
    branch: str = ""
    claimed_by: str = ""
    claimed_at: str = ""
    resources: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    @property
    def current(self) -> State:
        return State(self.state)

    def as_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class StateStore:
    """Filesystem-backed state. No database; a JSON file per task is enough."""

    def __init__(self, config: Config) -> None:
        self.root = config.state_dir / "tasks"

    def path_for(self, repo: RepoConfig, task_id: str) -> Path:
        return self.root / repo.name / f"{task_id}.json"

    def load(self, repo: RepoConfig, task_id: str) -> TaskState | None:
        path = self.path_for(repo, task_id)
        if not path.is_file():
            return None
        return TaskState(**json.loads(path.read_text(encoding="utf-8")))

    def get_or_create(self, repo: RepoConfig, task_id: str) -> TaskState:
        existing = self.load(repo, task_id)
        if existing is not None:
            return existing
        return TaskState(repo=repo.name, repo_identity=repo.identity, task_id=task_id)

    def save(self, repo: RepoConfig, state: TaskState) -> None:
        path = self.path_for(repo, state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state.as_dict(), indent=2), encoding="utf-8")

    def transition(
        self, repo: RepoConfig, state: TaskState, target: State, note: str = ""
    ) -> TaskState:
        """Move to ``target`` or refuse with ILLEGAL_TRANSITION."""
        if not can_transition(state.current, target):
            raise OrchestratorError(
                Reason.ILLEGAL_TRANSITION,
                f"{repo.name}/{state.task_id}: {state.current} -> {target} is not a legal "
                f"transition (legal: {sorted(str(s) for s in TRANSITIONS[state.current])})",
                {"from": str(state.current), "to": str(target)},
            )
        state.history.append(
            {"from": str(state.current), "to": str(target), "at": _utc_now(), "note": note}
        )
        state.state = str(target)
        self.save(repo, state)
        return state

    def active(self, repo: RepoConfig) -> dict[str, TaskState]:
        """Every task in this repository that currently occupies a working state."""
        directory = self.root / repo.name
        if not directory.is_dir():
            return {}
        inactive = {State.READY, State.BLOCKED}
        states = {}
        for path in sorted(directory.glob("*.json")):
            loaded = TaskState(**json.loads(path.read_text(encoding="utf-8")))
            if loaded.current not in inactive:
                states[loaded.task_id] = loaded
        return states

    def resource_holders(self, repo: RepoConfig) -> dict[str, list[str]]:
        """Which active tasks hold which repository-global resources."""
        holders: dict[str, list[str]] = {}
        for task_id, state in self.active(repo).items():
            for handle in state.resources:
                holders.setdefault(handle, []).append(task_id)
        return holders
