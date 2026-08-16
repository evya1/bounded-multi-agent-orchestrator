"""Configuration loading.

Everything the control plane treats as policy — which repositories exist, which
paths are governance, which risk level demands which human gate, which model
serves which role, what the daily budget is — lives in YAML, not in Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import OrchestratorError, Reason

WORKSPACE = Path(__file__).resolve().parent.parent
CONFIG_DIR = WORKSPACE / "config"


@dataclass(frozen=True)
class RepoConfig:
    """One role repository the orchestrator may drive."""

    name: str
    path: Path
    origin: str
    default_base: str = "origin/master"

    @property
    def identity(self) -> str:
        """Stable cross-repository identity used to bind approvals.

        The repository *name* alone is not enough: two clones could both be
        called ``police``. The origin URL pins the actual repository.
        """
        return f"{self.name}@{self.origin}"


@dataclass(frozen=True)
class ProjectConfig:
    """Where the authoritative project planning artifacts live inside a repo."""

    task_dirs: tuple[str, ...] = ("docs/tasks",)
    requirement_register: str = "docs/spec/CANONICAL_REQUIREMENTS.md"
    open_register: str = "docs/spec/OPEN_QUESTIONS.md"
    input_register: str = "docs/inputs/INPUT_REGISTER.md"
    decision_register: str = "docs/spec/OPEN_QUESTIONS.md"
    always_context: tuple[str, ...] = ("AGENTS.md",)
    body_reference_dirs: tuple[str, ...] = ("docs/decisions/", "docs/contracts/", "docs/mechanisms/")
    unresolved_decision_markers: tuple[str, ...] = ("TBD_TEAM_DECISION", "TBD", "—", "")
    resolved_input_statuses: tuple[str, ...] = ("RECEIVED", "VERIFIED", "RECEIVED_VERIFIED")
    open_items_heading: str = "Active OPEN items"
    decision_items_heading: str = "Implementation Decision Register"


@dataclass(frozen=True)
class BudgetConfig:
    daily_openrouter_budget_usd: float = 4.00
    reserve_usd: float = 0.75
    max_usd_per_call: float = 0.50
    estimate_output_tokens: int = 4000


@dataclass(frozen=True)
class Config:
    """The whole control-plane policy, loaded once."""

    workspace: Path
    repos: dict[str, RepoConfig]
    project: ProjectConfig
    governance_paths: tuple[str, ...]
    review_policy: dict[str, tuple[str, ...]]
    resources: dict[str, tuple[str, ...]]
    budget: BudgetConfig
    models: dict = field(default_factory=dict)

    def repo(self, name: str) -> RepoConfig:
        try:
            return self.repos[name]
        except KeyError:
            raise OrchestratorError(
                Reason.REPO_UNKNOWN,
                f"unknown repo {name!r}; configured: {sorted(self.repos)}",
            ) from None

    @property
    def state_dir(self) -> Path:
        return self.workspace / "state"

    @property
    def worktree_root(self) -> Path:
        return self.workspace / "worktrees"


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise OrchestratorError(Reason.CONFIG_INVALID, f"missing config file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OrchestratorError(Reason.CONFIG_INVALID, f"{path}: top level must be a mapping")
    return data


def load_models_config(config_dir: Path | None = None) -> dict:
    """Load the routing table, preferring a live ``models.yaml`` over the example."""
    directory = config_dir or CONFIG_DIR
    live = directory / "models.yaml"
    return _read_yaml(live if live.is_file() else directory / "models.example.yaml")


def load_config(config_path: Path | None = None, workspace: Path | None = None) -> Config:
    """Load ``config/orchestrator.yaml`` plus the model routing table."""
    root = workspace or WORKSPACE
    path = config_path or (root / "config" / "orchestrator.yaml")
    raw = _read_yaml(path)

    repos = {}
    for name, spec in (raw.get("repos") or {}).items():
        if "path" not in spec or "origin" not in spec:
            raise OrchestratorError(Reason.CONFIG_INVALID, f"repo {name!r} needs 'path' and 'origin'")
        repos[name] = RepoConfig(
            name=name,
            path=Path(spec["path"]).expanduser(),
            origin=str(spec["origin"]),
            default_base=str(spec.get("default_base", "origin/master")),
        )
    if not repos:
        raise OrchestratorError(Reason.CONFIG_INVALID, f"{path}: no repos configured")

    project_raw = raw.get("project") or {}
    tuple_fields = {
        "task_dirs",
        "always_context",
        "body_reference_dirs",
        "unresolved_decision_markers",
        "resolved_input_statuses",
    }
    project = ProjectConfig(
        **{k: (tuple(v) if k in tuple_fields else v) for k, v in project_raw.items()}
    )

    review_policy = {
        str(risk): tuple(gates or ())
        for risk, gates in (raw.get("review_policy") or {}).items()
    }
    resources = {
        str(name): tuple(paths or ())
        for name, paths in (raw.get("resources") or {}).items()
    }
    budget = BudgetConfig(**(raw.get("budget") or {}))

    return Config(
        workspace=root,
        repos=repos,
        project=project,
        governance_paths=tuple(raw.get("governance_paths") or ()),
        review_policy=review_policy,
        resources=resources,
        budget=budget,
        models=load_models_config(path.parent),
    )
