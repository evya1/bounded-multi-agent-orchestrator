"""Miniature Git repositories and task fixtures.

Nothing here touches the real Police / Thief / Bundle repositories. Every test
builds a throwaway repo in a tmp_path so that dangerous behaviour (write-set
violations, stale bases, forced pushes) can be exercised safely.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from orchestrator.config import BudgetConfig, Config, ProjectConfig, RepoConfig

GOVERNANCE = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs/PRD.md",
    "docs/PLAN.md",
    "docs/TODO.md",
    "docs/spec/",
    "docs/decisions/",
    "docs/changes/",
)

REVIEW_POLICY = {"high": ("plan", "diff"), "medium": ("diff",), "low": ()}
RESOURCES = {"dependency_manifest": ("pyproject.toml",), "dependency_lock": ("uv.lock",)}


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
    return proc.stdout


def write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return path


TASK_TEMPLATE = """
---
id: {task_id}
status: {status}
priority: P0
task_type: foundation
component: system
optional: false
implements:
{implements}
context_files:
  - docs/PRD.md
read_set:
  - config/repo_quality.toml
depends_on: {depends_on}
gates:
{gates}
parallel_safe: true
claimed_by:
claim_expires_at:
write_set:
{write_set}
risk: {risk}
---

# {task_id} — fixture task

References `docs/decisions/ADR-002-ci-uv-bootstrap.md` for the CI bootstrap.
It does not select the GUI toolkit, which `PLANQ-007` owns.

## Acceptance criteria

- [ ] The lock validates. `{{#dependency_lock}}`

## Verification

- `{verification}`
"""


def make_task(
    task_id: str = "T002",
    status: str = "ready",
    risk: str = "medium",
    implements: tuple[str, ...] = ("NET-001", "QR-014"),
    depends_on: tuple[str, ...] = (),
    gates: tuple[dict, ...] = ({"id": "PLANQ-002", "kind": "decision", "scope": "dependency_lock", "blocks": "criterion"},),
    write_set: tuple[str, ...] = ("pyproject.toml", "uv.lock"),
    verification: str = "true",
) -> str:
    gate_block = (
        "\n".join(
            "  - id: {id}\n    kind: {kind}\n    scope: {scope}\n    blocks: {blocks}".format(**gate)
            for gate in gates
        )
        or "  []"
    )
    return TASK_TEMPLATE.format(
        task_id=task_id,
        status=status,
        risk=risk,
        implements="\n".join(f"  - {r}" for r in implements) or "  []",
        depends_on=list(depends_on),
        gates=gate_block,
        write_set="\n".join(f"  - {w}" for w in write_set) or "  []",
        verification=verification,
    )


OPEN_REGISTER = """
# Open Questions

## Active OPEN items

| ID | Type | Question | Impact | Action | Owner |
|---|---|---|---|---|---|
| OPEN-001 | MISSING OFFICIAL INPUT | Official schemas absent. | Blocks T016. | Obtain them. | orchestrator |

## Input gates

| Gate | Class | Covers | Ready when |
|---|---|---|---|
| `G-OFFICIAL` | official artifact intake | OPEN-001 | Moodle supplies the file |
| `G-TEAM` | public team metadata | OPEN-003 | human confirmation |

## Implementation Decision Register

| ID | Planning question | Constraints | Decision | Owner | Affected tasks |
|---|---|---|---|---|---|
| PLANQ-002 | Which dependency baseline? | Smallest set. | `TBD_TEAM_DECISION` | project team | T002 |
| PLANQ-007 | Which GUI toolkit? | Local truth only. | `TBD_TEAM_DECISION` | project team | T014 |
| PLANQ-009 | Which formatter? | Any. | `ruff-format` | project team | T003 |
"""

REQUIREMENTS = """
# Canonical Requirements

| ID | Normative level | Canonical requirement | Authority | Official source |
|---|---|---|---|---|
| NET-001 | MUST | Each peer MUST expose MCP tools via FastMCP. | PROJECT SPECIFICATION | §2.3 |
| QR-014 | MUST | uv MUST be the package manager; uv.lock after decisions. | QUALITY MODEL | §8.4 |
| GAME-001 | MUST | The board MUST be square and at least 7x7. | PROJECT SPECIFICATION | App. F |
| STRAT-003 | SHOULD | Scent decays. | PROJECT SPECIFICATION | §4.3 |
"""

INPUT_REGISTER = """
# Official Input Register

| Input ID | Artifact | Authority | Status | Hash | Date | OPEN IDs | Requirements | Gate | Notes |
|---|---|---|---|---|---|---|---|---|---|
| INPUT-001 | Official JSON schemas | Course staff | MISSING | — | — | OPEN-001 | NET-001 | G-OFFICIAL | Do not fabricate. |
| INPUT-009 | Team metadata record | Project team | RECEIVED | — | — | OPEN-003 | NET-001 | G-TEAM | Public only. |
"""


def build_repo(root: Path, name: str, tasks: dict[str, str] | None = None) -> Path:
    """A miniature repository shaped like the real Police/Thief planning scaffold."""
    repo = root / name
    repo.mkdir(parents=True)
    git(["init", "-q"], repo)
    git(["symbolic-ref", "HEAD", "refs/heads/master"], repo)  # git 2.25 has no `init -b`
    git(["config", "user.email", "harness@example.invalid"], repo)
    git(["config", "user.name", "harness"], repo)
    git(["config", "commit.gpgsign", "false"], repo)
    git(["remote", "add", "origin", f"https://github.com/example/{name}.git"], repo)

    write(repo, "AGENTS.md", f"# AGENTS.md\n\nRepository purpose: {name} peer.\n")
    write(repo, "CONTRIBUTING.md", "# Contributing\n")
    write(repo, "docs/PRD.md", "# PRD\n\nSystem product requirements.\n")
    write(repo, "docs/PLAN.md", "# PLAN\n\nSystem technical strategy.\n")
    write(repo, "docs/TODO.md", "# TODO\n")
    write(repo, "docs/spec/CANONICAL_REQUIREMENTS.md", REQUIREMENTS)
    write(repo, "docs/spec/OPEN_QUESTIONS.md", OPEN_REGISTER)
    write(repo, "docs/inputs/INPUT_REGISTER.md", INPUT_REGISTER)
    write(repo, "docs/decisions/ADR-002-ci-uv-bootstrap.md", "# ADR-002\n\nPinned uv bootstrap.\n")
    write(repo, "docs/decisions/ADR-009-unrelated.md", "# ADR-009\n\nUnrelated decision.\n")
    write(repo, "docs/components/C01-game-core/PRD.md", "# C01 PRD\n")
    write(repo, "config/repo_quality.toml", 'python_version = "3.12"\n')
    write(repo, "pyproject.toml", "[project]\nname = 'fixture'\n")

    for task_id, body in (tasks or {"T002": make_task()}).items():
        write(repo, f"docs/tasks/{task_id}-fixture.md", body)

    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "initial planning scaffold"], repo)
    # Give the fixture an origin/master ref without a network remote.
    git(["update-ref", "refs/remotes/origin/master", "HEAD"], repo)
    return repo


MODELS_CONFIG = {
    "adapters": {"fake": {"enabled": True}},
    "providers": {
        "local": {"kind": "local", "paid": False, "pi_provider": "llama.cpp", "model_id": "qwen-local",
                  "base_url_env": "FIXTURE_LOCAL_URL", "default_base_url": ""},
        "cloud": {"kind": "cloud", "paid": True, "pi_provider": "openrouter", "api_key_env": "FIXTURE_KEY"},
    },
    "models": {
        "local-qwen": {"provider": "local", "family": "qwen", "cost_usd_per_mtok": {"input": 0.0, "output": 0.0}},
        "cheap-cloud": {"provider": "cloud", "id": "vendor/cheap", "family": "deepseek",
                        "cost_usd_per_mtok": {"input": 1.0, "output": 2.0}},
        "frontier": {"provider": "cloud", "id": "vendor/frontier", "family": "openai",
                     "cost_usd_per_mtok": {"input": 2.5, "output": 15.0}},
        "absent": {"provider": "cloud", "id": "vendor/absent", "family": "openai",
                   "cost_usd_per_mtok": {"input": 1.0, "output": 1.0}},
    },
    "roles": {
        "local_executor": {"primary": "local-qwen", "fallbacks": [], "allow_weaker_fallback": False},
        "value_reasoner": {"primary": "cheap-cloud", "fallbacks": ["frontier"], "allow_weaker_fallback": True},
        "code_reviewer": {"primary": "frontier", "fallbacks": [], "allow_weaker_fallback": False},
        "strict_absent": {"primary": "absent", "fallbacks": ["cheap-cloud"], "allow_weaker_fallback": False},
    },
    "escalation": {
        "low": {"plan": "local_executor", "implement": "local_executor", "review": "value_reasoner"},
        "medium": {"plan": "value_reasoner", "implement": "local_executor", "review": "code_reviewer"},
        "high": {"plan": "code_reviewer", "implement": "local_executor", "review": "code_reviewer"},
    },
    "privileges": {
        "plan": {"tools": "none", "bash": False},
        "review": {"tools": "none", "bash": False},
        "implement": {"tools": "edit", "bash": False, "write_set_only": True},
    },
    "diversity": {"enforce_for": ["high"]},
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "config").mkdir(parents=True)
    (root / "config" / "models.example.yaml").write_text(yaml.safe_dump(MODELS_CONFIG))
    return root


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    return {
        "police": build_repo(tmp_path / "repos", "police"),
        "thief": build_repo(tmp_path / "repos", "thief"),
    }


@pytest.fixture
def config(workspace: Path, repos: dict[str, Path]) -> Config:
    return Config(
        workspace=workspace,
        repos={
            name: RepoConfig(name, path, f"https://github.com/example/{name}.git", "origin/master")
            for name, path in repos.items()
        },
        project=ProjectConfig(),
        governance_paths=GOVERNANCE,
        review_policy=REVIEW_POLICY,
        resources=RESOURCES,
        budget=BudgetConfig(),
        models=MODELS_CONFIG,
    )
