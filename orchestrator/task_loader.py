"""Task frontmatter parsing with a real YAML parser.

v3 hand-rolled a line-oriented parser that flattened nested structures. The
project's ``gates:`` field is a list of mappings and must survive parsing as
structured data — a gate whose ``blocks`` level is lost becomes a gate that
cannot be enforced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import OrchestratorError, Reason

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_ANCHOR = re.compile(r"\{#([a-z0-9_]+)\}")


@dataclass(frozen=True)
class Gate:
    """One project gate declared in a task's frontmatter.

    ``blocks`` is the whole point: only ``start`` prevents claiming.
    """

    id: str
    kind: str
    blocks: str
    scope: str | None = None

    @property
    def blocks_start(self) -> bool:
        return self.blocks == "start"

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "blocks": self.blocks, "scope": self.scope}


@dataclass(frozen=True)
class Task:
    """One parsed T### task file."""

    id: str
    path: Path
    frontmatter: dict
    body: str

    def _list(self, key: str) -> list[str]:
        value = self.frontmatter.get(key) or []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status") or "unknown")

    @property
    def risk(self) -> str:
        return str(self.frontmatter.get("risk") or "high")

    @property
    def priority(self) -> str:
        return str(self.frontmatter.get("priority") or "?")

    @property
    def task_type(self) -> str:
        return str(self.frontmatter.get("task_type") or "unknown")

    @property
    def component(self) -> str:
        return str(self.frontmatter.get("component") or "unknown")

    @property
    def optional(self) -> bool:
        return bool(self.frontmatter.get("optional") or False)

    @property
    def parallel_safe(self) -> bool:
        return bool(self.frontmatter.get("parallel_safe") or False)

    @property
    def implements(self) -> list[str]:
        return self._list("implements")

    @property
    def context_files(self) -> list[str]:
        return self._list("context_files")

    @property
    def read_set(self) -> list[str]:
        return self._list("read_set")

    @property
    def write_set(self) -> list[str]:
        return self._list("write_set")

    @property
    def depends_on(self) -> list[str]:
        return self._list("depends_on")

    @property
    def claimed_by(self) -> str | None:
        value = self.frontmatter.get("claimed_by")
        return str(value) if value else None

    @property
    def gates(self) -> list[Gate]:
        gates: list[Gate] = []
        raw = self.frontmatter.get("gates") or []
        if not isinstance(raw, list):
            raise OrchestratorError(
                Reason.CONFIG_INVALID,
                f"{self.id}: 'gates' must be a list of mappings, got {type(raw).__name__}",
            )
        for entry in raw:
            if not isinstance(entry, dict):
                raise OrchestratorError(
                    Reason.CONFIG_INVALID,
                    f"{self.id}: gate entry must be a mapping, got {entry!r} "
                    "(nested gate structure was flattened)",
                )
            gates.append(
                Gate(
                    id=str(entry.get("id")),
                    kind=str(entry.get("kind")),
                    blocks=str(entry.get("blocks")),
                    scope=str(entry["scope"]) if entry.get("scope") else None,
                )
            )
        return gates

    @property
    def anchors(self) -> set[str]:
        """Acceptance-criterion anchors ``{#name}`` a criterion gate can target."""
        return set(_ANCHOR.findall(self.body))

    @property
    def verification_commands(self) -> list[str]:
        """Commands from the task's own ``## Verification`` section.

        The deterministic verifier runs these; a model worker never does.
        """
        commands: list[str] = []
        in_section = False
        for line in self.body.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                in_section = stripped.lower().startswith("## verification")
                continue
            if in_section and stripped.startswith("- `") and stripped.endswith("`"):
                commands.append(stripped[3:-1])
        return commands


def parse_task(path: Path) -> Task:
    """Parse one task file. Fails loudly on absent or non-mapping frontmatter."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        raise OrchestratorError(Reason.CONFIG_INVALID, f"{path}: no YAML frontmatter block")
    frontmatter = yaml.safe_load(match.group(1))
    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        raise OrchestratorError(Reason.CONFIG_INVALID, f"{path}: frontmatter must be a mapping")
    return Task(
        id=str(frontmatter.get("id") or path.stem),
        path=path,
        frontmatter=frontmatter,
        body=match.group(2),
    )


def load_tasks(repo_path: Path, task_dirs: tuple[str, ...]) -> dict[str, Task]:
    """Load every task file under the configured task directories."""
    tasks: dict[str, Task] = {}
    for relative in task_dirs:
        directory = repo_path / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("T*.md")):
            task = parse_task(path)
            tasks[task.id] = task
    return tasks


def get_task(tasks: dict[str, Task], task_id: str) -> Task:
    try:
        return tasks[task_id]
    except KeyError:
        raise OrchestratorError(
            Reason.TASK_NOT_FOUND, f"no task {task_id!r} (known: {len(tasks)} tasks)"
        ) from None
