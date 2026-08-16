"""Bounded filesystem write policy — the authoritative implementation.

``cwd`` is not a sandbox and ``--tools`` only selects tool *names*, not paths.
This module is the single source of truth for "may this path be mutated?", and
it is enforced twice:

* **before** the write, by the Pi extension in ``pi_ext/write_guard.ts``, which
  blocks the ``tool_call`` and mirrors these rules exactly;
* **after** the run, by the deterministic Git audit in ``verifier``.

The pre-write hook is the enforcement. The post-run audit is defence in depth;
it is not a substitute, because by the time it runs the write has happened.

A path is mutable only if BOTH hold:

1. it resolves inside the task worktree, after symlink resolution;
2. it is covered by the task's declared ``write_set``.

Everything else — absolute paths outside the tree, ``../`` traversal, symlinks
pointing out, undeclared siblings — is refused.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

#: Pi 0.84.2 built-ins, verified against dist/core/tools/*.js.
READ_ONLY_TOOLS = frozenset({"read", "ls", "find", "grep"})
MUTATION_TOOLS = frozenset({"write", "edit"})
SHELL_TOOLS = frozenset({"bash"})

#: Env vars the Pi extension reads. Non-secret: a path and a path list.
ENV_ROOT = "ORCHESTRATOR_WRITE_ROOT"
ENV_WRITE_SET = "ORCHESTRATOR_WRITE_SET"
ENV_ALLOW_SHELL = "ORCHESTRATOR_ALLOW_SHELL"


class Denial(StrEnum):
    """Why a path was refused. Mirrored verbatim by the TypeScript guard."""

    OUTSIDE_WORKTREE = "OUTSIDE_WORKTREE"
    NOT_IN_WRITE_SET = "NOT_IN_WRITE_SET"
    SYMLINK_ESCAPE = "SYMLINK_ESCAPE"
    EMPTY_PATH = "EMPTY_PATH"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    NO_PATH_ARGUMENT = "NO_PATH_ARGUMENT"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    denial: Denial | None = None
    resolved: str = ""

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "denial": str(self.denial) if self.denial else None,
            "resolved": self.resolved,
        }


def _resolve_existing_ancestor(path: Path) -> Path:
    """Resolve symlinks as far as the path actually exists, keep the rest literal.

    A file the task is about to create does not exist yet, so ``Path.resolve``
    on the whole path would not follow a symlinked *parent*. Resolving the
    deepest existing ancestor and re-appending the remainder catches
    ``worktree/link-to-etc/passwd`` where ``link-to-etc`` is a symlink out.
    """
    remainder: list[str] = []
    current = path
    while True:
        if current.exists() or current.is_symlink():
            return Path(os.path.realpath(current)).joinpath(*reversed(remainder))
        if current.parent == current:
            return Path(os.path.normpath(path))
        remainder.append(current.name)
        current = current.parent


def _covered(relative: str, write_set: list[str]) -> bool:
    """Is a worktree-relative POSIX path covered by a write_set entry?"""
    for declared in write_set:
        entry = declared.strip()
        if not entry:
            continue
        entry = entry.rstrip("/")
        if relative == entry:
            return True
        if relative.startswith(entry + "/"):
            return True
    return False


@dataclass(frozen=True)
class PathPolicy:
    """The mutation policy for one task: one worktree, one write_set."""

    root: Path
    write_set: tuple[str, ...]

    @property
    def real_root(self) -> Path:
        return Path(os.path.realpath(self.root))

    def check_path(self, raw: str, cwd: Path | None = None) -> Verdict:
        """May this path be mutated? ``raw`` is whatever the model supplied."""
        if not raw or not str(raw).strip():
            return Verdict(False, "empty path", Denial.EMPTY_PATH)

        base = Path(cwd) if cwd is not None else self.root
        candidate = Path(raw)
        absolute = candidate if candidate.is_absolute() else base / candidate

        resolved = _resolve_existing_ancestor(absolute)
        root = self.real_root

        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            # Distinguish "you asked for a path outside" from "a symlink took
            # you outside", because the operator needs to know which happened.
            lexical = Path(os.path.normpath(str(absolute)))
            inside_lexically = lexical == root or root in lexical.parents
            denial = Denial.SYMLINK_ESCAPE if inside_lexically else Denial.OUTSIDE_WORKTREE
            return Verdict(
                False,
                f"{raw!r} resolves to {resolved} which is outside the task worktree {root}",
                denial,
                str(resolved),
            )

        if relative in ("", "."):
            return Verdict(False, "the worktree root itself is not writable", Denial.NOT_IN_WRITE_SET, str(resolved))

        if not _covered(relative, list(self.write_set)):
            return Verdict(
                False,
                f"{relative!r} is not covered by the declared write_set {list(self.write_set)}",
                Denial.NOT_IN_WRITE_SET,
                str(resolved),
            )
        return Verdict(True, f"{relative!r} is inside the worktree and owned by this task", None, str(resolved))

    def check_tool_call(self, tool_name: str, arguments: dict, cwd: Path | None = None) -> Verdict:
        """Deny by default: an unrecognised tool is refused, not waved through.

        This is deliberately a positive-security model. If a future Pi version
        adds a mutation tool this guard has never heard of, the guard blocks it
        rather than silently permitting a new write path.
        """
        if tool_name in READ_ONLY_TOOLS:
            return Verdict(True, f"{tool_name} is read-only")
        if tool_name in SHELL_TOOLS:
            return Verdict(
                False,
                "the implementer stage runs without shell access",
                Denial.TOOL_NOT_ALLOWED,
            )
        if tool_name not in MUTATION_TOOLS:
            return Verdict(
                False,
                f"tool {tool_name!r} is not on the bounded implementer allowlist",
                Denial.TOOL_NOT_ALLOWED,
            )
        raw = arguments.get("path") or arguments.get("file_path")
        if not raw:
            return Verdict(
                False,
                f"{tool_name} call carries no path argument",
                Denial.NO_PATH_ARGUMENT,
            )
        return self.check_path(str(raw), cwd)

    def environment(self, allow_shell: bool = False) -> dict[str, str]:
        """Env handed to the Pi process so the extension enforces this policy.

        Contains a directory path and a list of repo-relative paths. No secrets.
        """
        return {
            ENV_ROOT: str(self.real_root),
            ENV_WRITE_SET: json.dumps(list(self.write_set)),
            ENV_ALLOW_SHELL: "1" if allow_shell else "0",
        }


def policy_for(worktree: Path, write_set: list[str]) -> PathPolicy:
    """Build the policy for a task, normalising declared write_set entries."""
    normalised = tuple(PurePosixPath(entry.strip()).as_posix().rstrip("/") for entry in write_set if entry.strip())
    return PathPolicy(root=Path(worktree), write_set=normalised)


EXTENSION_PATH = Path(__file__).resolve().parent / "pi_ext" / "write_guard.ts"
