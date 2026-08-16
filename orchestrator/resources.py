"""Repository-global exclusive resources.

Some artifacts are whole-environment integration points, not ordinary files.
``pyproject.toml`` and ``uv.lock`` describe the entire repository's runtime; two
tasks mutating them concurrently produce a lock that reflects neither. T002 owns
their initial creation, and every later dependency mutation must be explicitly
serialized.

Resources are per repository. Police's ``dependency_lock`` and Thief's
``dependency_lock`` are different resources and may be held at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .task_loader import Task


@dataclass(frozen=True, order=True)
class ResourceHandle:
    """One exclusive resource instance, scoped to a single repository."""

    repo: str
    resource: str

    def __str__(self) -> str:
        return f"{self.repo}:{self.resource}"


def _covers(declared: str, resource_path: str) -> bool:
    """Does a write_set entry cover a resource path? Directories cover children."""
    if declared == resource_path:
        return True
    prefix = declared if declared.endswith("/") else declared + "/"
    return resource_path.startswith(prefix)


def required_resources(
    repo: str, task: Task, resources: dict[str, tuple[str, ...]]
) -> list[ResourceHandle]:
    """Which exclusive resources this task must hold, from its write_set alone."""
    held: list[ResourceHandle] = []
    for name, paths in resources.items():
        if any(_covers(declared, path) for declared in task.write_set for path in paths):
            held.append(ResourceHandle(repo, name))
    return sorted(set(held))


def conflicts(
    wanted: list[ResourceHandle], holders: dict[str, list[str]]
) -> list[tuple[ResourceHandle, list[str]]]:
    """Resources already held by another active task in the same repository.

    ``holders`` maps ``str(ResourceHandle)`` to the task IDs currently holding it.
    """
    found = []
    for handle in wanted:
        current = holders.get(str(handle)) or []
        if current:
            found.append((handle, sorted(current)))
    return found
