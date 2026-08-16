"""Bounded context compiler.

Compiles exactly what a model would receive, without calling any model. The
rule this enforces comes from the repository's own AGENTS.md: a worker reads
AGENTS.md, its own task file, the task's declared ``context_files``, the
requirement / OPEN / input / decision IDs the task *names in frontmatter*, its
``read_set``, and the current contents of its ``write_set``.

Two deliberate boundaries:

* IDs are taken from structured frontmatter only. ``PLANQ-007`` appearing in a
  task's prose ("the GUI toolkit, which PLANQ-007 owns") is a scope *exclusion*
  statement, not a request for that material. Such IDs are recorded as EXCLUDED
  with a reason, never silently pulled in.
* A missing declared context file is an error, not an invitation to search. If
  the compiler cannot justify a file, it does not include it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .config import Config, RepoConfig
from .errors import OrchestratorError, Reason, Refusal
from .registers import Registers
from .task_loader import Task

_BACKTICKED_PATH = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|toml|yaml|yml|py|cfg|txt))`")


class Kind(StrEnum):
    FULL_FILE = "FULL_FILE"
    TARGETED = "TARGETED"
    WRITE_OWNED = "WRITE_OWNED"
    MISSING = "MISSING"
    EXCLUDED = "EXCLUDED"


class Access(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE = "WRITE"
    NONE = "NONE"


@dataclass
class Item:
    """One entry in the compiled context manifest."""

    kind: Kind
    access: Access
    ref: str                 # repo-relative path, or an ID for targeted excerpts
    reason: str
    source: str | None = None    # file an excerpt came from
    line: int | None = None
    bytes: int = 0
    text: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "access": str(self.access),
            "ref": self.ref,
            "reason": self.reason,
            "source": self.source,
            "line": self.line,
            "bytes": self.bytes,
        }


@dataclass
class ContextManifest:
    """The complete, auditable answer to 'what would the model see?'."""

    repo: str
    repo_identity: str
    task_id: str
    base_sha: str
    tree: str = ""
    items: list[Item] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)

    def of_kind(self, *kinds: Kind) -> list[Item]:
        return [item for item in self.items if item.kind in kinds]

    @property
    def included(self) -> list[Item]:
        return self.of_kind(Kind.FULL_FILE, Kind.TARGETED, Kind.WRITE_OWNED)

    @property
    def included_refs(self) -> list[str]:
        return [item.ref for item in self.included]

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.included)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "repo_identity": self.repo_identity,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "tree": self.tree,
            "total_bytes": self.total_bytes,
            "included_count": len(self.included),
            "items": [item.as_dict() for item in self.items],
            "refusals": [refusal.as_dict() for refusal in self.refusals],
        }

    def render_prompt_context(self) -> str:
        """The literal context block handed to a model."""
        blocks = []
        for item in self.included:
            if item.text is None:
                continue
            header = f"===== {item.ref} ({item.kind}, {item.access}) ====="
            blocks.append(f"{header}\n{item.text}")
        return "\n\n".join(blocks)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class ContextCompiler:
    """Builds the bounded manifest for one (repo, task) pair."""

    def __init__(
        self,
        config: Config,
        repo: RepoConfig,
        registers: Registers,
        tree: Path | None = None,
    ) -> None:
        """``repo`` supplies IDENTITY; ``tree`` supplies CONTENT.

        A model stage works inside a task worktree, so its context must be read
        from that worktree — otherwise the manifest describes a different tree
        from the one being edited. The manifest still records the original
        repository identity, so a worktree never becomes a separate repo for
        approval-binding purposes.
        """
        self.config = config
        self.repo = repo
        self.registers = registers
        self.project = config.project
        self.tree = Path(tree) if tree is not None else repo.path

    def compile(self, task: Task, base_sha: str, strict: bool = True) -> ContextManifest:
        manifest = ContextManifest(
            repo=self.repo.name,
            repo_identity=self.repo.identity,
            task_id=task.id,
            base_sha=base_sha,
            tree=str(self.tree),
        )
        self._add_always(manifest)
        self._add_task_file(manifest, task)
        self._add_declared_files(manifest, task)
        self._add_requirements(manifest, task)
        self._add_gates(manifest, task)
        self._add_body_references(manifest, task)
        self._add_write_set(manifest, task)
        self._record_exclusions(manifest, task)

        if strict and manifest.refusals:
            raise OrchestratorError(
                Reason.CONTEXT_COMPILE_FAILED,
                f"{self.repo.name}/{task.id}: bounded context could not be compiled; "
                + "; ".join(str(refusal) for refusal in manifest.refusals),
                {"refusals": [refusal.as_dict() for refusal in manifest.refusals]},
            )
        return manifest

    # ------------------------------------------------------------- collectors

    def _file_item(self, relative: str, access: Access, reason: str) -> Item:
        path = self.tree / relative
        if not path.is_file():
            return Item(Kind.MISSING, access, relative, reason)
        text = _read(path)
        return Item(Kind.FULL_FILE, access, relative, reason, bytes=len(text.encode()), text=text)

    def _add_always(self, manifest: ContextManifest) -> None:
        for relative in self.project.always_context:
            item = self._file_item(relative, Access.READ_ONLY, "AGENTS.md contract: always in scope")
            if item.kind is Kind.MISSING:
                manifest.refusals.append(
                    Refusal(Reason.CONTEXT_FILE_MISSING, f"required context file absent: {relative}")
                )
            manifest.items.append(item)

    def _add_task_file(self, manifest: ContextManifest, task: Task) -> None:
        try:
            relative = task.path.relative_to(self.tree).as_posix()
        except ValueError:
            relative = task.path.name
        text = _read(task.path)
        manifest.items.append(
            Item(
                Kind.FULL_FILE,
                Access.READ_ONLY,
                relative,
                "the claimed task",
                bytes=len(text.encode()),
                text=text,
            )
        )

    def _add_declared_files(self, manifest: ContextManifest, task: Task) -> None:
        for relative in task.context_files:
            item = self._file_item(relative, Access.READ_ONLY, "task frontmatter: context_files")
            if item.kind is Kind.MISSING:
                manifest.refusals.append(
                    Refusal(
                        Reason.CONTEXT_FILE_MISSING,
                        f"{task.id}: context_files declares a path that does not exist: {relative}",
                        {"path": relative, "field": "context_files"},
                    )
                )
            manifest.items.append(item)
        for relative in task.read_set:
            item = self._file_item(relative, Access.READ_ONLY, "task frontmatter: read_set")
            if item.kind is Kind.MISSING:
                manifest.refusals.append(
                    Refusal(
                        Reason.CONTEXT_FILE_MISSING,
                        f"{task.id}: read_set declares a path that does not exist: {relative}",
                        {"path": relative, "field": "read_set"},
                    )
                )
            manifest.items.append(item)

    def _add_requirements(self, manifest: ContextManifest, task: Task) -> None:
        for requirement_id in task.implements:
            excerpt = self.registers.requirement(requirement_id)
            if excerpt is None:
                manifest.refusals.append(
                    Refusal(
                        Reason.CONTEXT_ID_UNRESOLVED,
                        f"{task.id}: implements {requirement_id}, absent from "
                        f"{self.project.requirement_register}",
                        {"id": requirement_id},
                    )
                )
                manifest.items.append(
                    Item(Kind.MISSING, Access.READ_ONLY, requirement_id, "implements: unresolved ID")
                )
                continue
            manifest.items.append(
                Item(
                    Kind.TARGETED,
                    Access.READ_ONLY,
                    requirement_id,
                    "task frontmatter: implements (this row only)",
                    source=excerpt.source,
                    line=excerpt.line,
                    bytes=len(excerpt.text.encode()),
                    text=excerpt.text,
                )
            )

    def _add_gates(self, manifest: ContextManifest, task: Task) -> None:
        for gate in task.gates:
            excerpt = (
                self.registers.input_item(gate.id)
                if gate.kind == "input"
                else self.registers.open_item(gate.id)
            )
            if excerpt is None:
                manifest.items.append(
                    Item(
                        Kind.TARGETED if gate.kind == "input_gate" else Kind.MISSING,
                        Access.READ_ONLY,
                        gate.id,
                        f"task frontmatter: gates (kind={gate.kind}, blocks={gate.blocks}); "
                        "no single register row — gate class resolved by a human",
                    )
                )
                continue
            manifest.items.append(
                Item(
                    Kind.TARGETED,
                    Access.READ_ONLY,
                    gate.id,
                    f"task frontmatter: gates (kind={gate.kind}, blocks={gate.blocks}) — this row only",
                    source=excerpt.source,
                    line=excerpt.line,
                    bytes=len(excerpt.text.encode()),
                    text=excerpt.text,
                )
            )

    def _add_body_references(self, manifest: ContextManifest, task: Task) -> None:
        """Boundary contracts / ADRs / mechanisms the task body names by PATH.

        Only paths under configured directories, only when they exist, and only
        when written as an explicit backticked path. An ID in prose is not a
        reference — see ``_record_exclusions``.
        """
        already = {item.ref for item in manifest.items}
        for candidate in sorted(set(_BACKTICKED_PATH.findall(task.body))):
            if not candidate.startswith(tuple(self.project.body_reference_dirs)):
                continue
            if candidate in already:
                continue
            path = self.tree / candidate
            if not path.is_file():
                manifest.items.append(
                    Item(
                        Kind.EXCLUDED,
                        Access.NONE,
                        candidate,
                        "named in task body but no such file in this repository",
                    )
                )
                continue
            text = _read(path)
            manifest.items.append(
                Item(
                    Kind.FULL_FILE,
                    Access.READ_ONLY,
                    candidate,
                    "boundary contract / decision explicitly referenced by path in the task body",
                    bytes=len(text.encode()),
                    text=text,
                )
            )

    def _add_write_set(self, manifest: ContextManifest, task: Task) -> None:
        for relative in task.write_set:
            path = self.tree / relative
            if path.is_dir():
                manifest.items.append(
                    Item(Kind.WRITE_OWNED, Access.WRITE, relative, "write_set: existing directory")
                )
                continue
            if not path.is_file():
                manifest.items.append(
                    Item(
                        Kind.MISSING,
                        Access.WRITE,
                        relative,
                        "write_set: does not exist yet — this task creates it",
                    )
                )
                continue
            text = _read(path)
            manifest.items.append(
                Item(
                    Kind.WRITE_OWNED,
                    Access.WRITE,
                    relative,
                    "write_set: current contents",
                    bytes=len(text.encode()),
                    text=text,
                )
            )

    def _record_exclusions(self, manifest: ContextManifest, task: Task) -> None:
        """State, explicitly, the broad material this manifest does NOT include."""
        declared_ids = {gate.id for gate in task.gates} | set(task.implements)
        for mentioned in sorted(self.registers.mentioned_ids(task.body) - declared_ids):
            manifest.items.append(
                Item(
                    Kind.EXCLUDED,
                    Access.NONE,
                    mentioned,
                    "mentioned in task prose but not declared in frontmatter "
                    "(implements/gates) — usually a scope exclusion, never auto-loaded",
                )
            )

        requirement_ids = self.registers.requirement_ids()
        unrelated = sorted(requirement_ids - set(task.implements))
        if unrelated:
            manifest.items.append(
                Item(
                    Kind.EXCLUDED,
                    Access.NONE,
                    f"{self.project.requirement_register}#unrelated",
                    f"{len(unrelated)} canonical requirement(s) this task does not implement "
                    f"(of {len(requirement_ids)} total); e.g. {unrelated[:5]}",
                )
            )

        task_dir = self.project.task_dirs[0]
        others = sorted(
            path.name
            for path in (self.tree / task_dir).glob("T*.md")
            if not path.name.startswith(task.id)
        )
        if others:
            manifest.items.append(
                Item(
                    Kind.EXCLUDED,
                    Access.NONE,
                    f"{task_dir}/#other-tasks",
                    f"{len(others)} other task file(s) in the graph",
                )
            )

        components_dir = self.tree / "docs" / "components"
        if components_dir.is_dir() and task.component == "system":
            manifest.items.append(
                Item(
                    Kind.EXCLUDED,
                    Access.NONE,
                    "docs/components/",
                    "component PRD/PLAN set — this task's component is 'system'; "
                    "no component document is declared in context_files",
                )
            )

        manifest.items.append(
            Item(
                Kind.EXCLUDED,
                Access.NONE,
                "<bundle>/planning/, <bundle>/requirements/",
                "bundle-wide planning masters live outside this repository; AGENTS.md "
                "requires resolving everything from the repository-local copies",
            )
        )


def render_human(manifest: ContextManifest) -> str:
    """The human-readable manifest."""
    lines = [
        f"BOUNDED CONTEXT  repo={manifest.repo}  task={manifest.task_id}",
        f"repo identity:   {manifest.repo_identity}",
        f"base commit:     {manifest.base_sha}",
        f"read from tree:  {manifest.tree}",
        "",
    ]
    groups = [
        ("FULL FILES", [i for i in manifest.items if i.kind is Kind.FULL_FILE]),
        ("TARGETED IDS / EXCERPTS", [i for i in manifest.items if i.kind is Kind.TARGETED]),
        ("WRITE-OWNED", [i for i in manifest.items if i.kind is Kind.WRITE_OWNED]),
        ("MISSING", [i for i in manifest.items if i.kind is Kind.MISSING]),
        ("EXCLUDED", [i for i in manifest.items if i.kind is Kind.EXCLUDED]),
    ]
    for title, items in groups:
        lines.append(f"--- {title} ({len(items)}) ---")
        if not items:
            lines.append("    (none)")
        for item in items:
            where = f" [{item.source}:{item.line}]" if item.source else ""
            size = f" {item.bytes}B" if item.bytes else ""
            lines.append(f"    {item.access:<10} {item.ref}{where}{size}")
            lines.append(f"               reason: {item.reason}")
        lines.append("")
    lines.append(
        f"TOTAL INCLUDED: {len(manifest.included)} item(s), {manifest.total_bytes} bytes"
    )
    if manifest.refusals:
        lines.append("")
        lines.append("REFUSALS:")
        lines.extend(f"    {refusal}" for refusal in manifest.refusals)
    return "\n".join(lines)
