"""Deterministic verification and write-set enforcement.

Two rules shape this module.

1. Models do not decide whether verification passed. Exit codes do. A model may
   later diagnose a failure, but the pass/fail verdict is never its output.
2. Verification commands are themselves capable of writing files. So the
   worktree's change manifest is captured before the run and compared after, and
   any artifact the verification itself produced outside the write_set is
   REPORTED. Nothing is silently deleted.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gitio
from .errors import Reason, Refusal

IGNORED_PREFIXES = (".agent/",)


def _covered(path: str, write_set: list[str]) -> bool:
    for declared in write_set:
        if path == declared:
            return True
        prefix = declared if declared.endswith("/") else declared + "/"
        if path.startswith(prefix):
            return True
    return False


def audit_write_set(
    worktree: Path, base_sha: str, write_set: list[str]
) -> tuple[list[gitio.Change], list[Refusal]]:
    """Every change from any origin, and a refusal per path outside the write_set.

    Covers committed, staged, unstaged and untracked changes, plus deletions and
    renames — a rename is audited on BOTH its old and new path, because moving a
    file out of the write_set mutates a path the task does not own.
    """
    changes = gitio.all_changes(worktree, base_sha, IGNORED_PREFIXES)
    refusals = []
    for change in changes:
        for path in change.paths:
            if _covered(path, write_set):
                continue
            refusals.append(
                Refusal(
                    Reason.OUT_OF_WRITE_SET,
                    f"{path}: {change.kind.name} ({change.origin}) is outside the declared write_set",
                    {
                        "path": path,
                        "kind": change.kind.name,
                        "origin": change.origin,
                        "write_set": write_set,
                    },
                )
            )
    return changes, refusals


@dataclass
class CommandResult:
    command: str
    exit_code: int
    duration_s: float
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 2),
            "passed": self.passed,
            "output_tail": self.output_tail,
        }


@dataclass
class VerificationReport:
    repo: str
    task_id: str
    base_sha: str
    commands: list[CommandResult] = field(default_factory=list)
    write_set_refusals: list[Refusal] = field(default_factory=list)
    side_effects: list[Refusal] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            bool(self.commands)
            and all(result.passed for result in self.commands)
            and not self.write_set_refusals
            and not self.side_effects
        )

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "passed": self.passed,
            "changed_paths": self.changed_paths,
            "commands": [result.as_dict() for result in self.commands],
            "write_set_refusals": [r.as_dict() for r in self.write_set_refusals],
            "side_effects": [r.as_dict() for r in self.side_effects],
        }


def _run(command: str, cwd: Path, timeout: int) -> CommandResult:
    """Run one task-declared verification command.

    ``shell=True`` is deliberate and documented: the commands come verbatim from
    the task's own ``## Verification`` section, authored as shell strings
    (``uv run pytest``). Re-tokenising them here would silently change what the
    project asked to be run. They execute inside the task's worktree with the
    orchestrator's own privileges — no model chooses them.
    """
    started = time.monotonic()
    proc = subprocess.run(
        command, cwd=cwd, shell=True, text=True, capture_output=True, check=False, timeout=timeout
    )
    return CommandResult(
        command=command,
        exit_code=proc.returncode,
        duration_s=time.monotonic() - started,
        output_tail=(proc.stdout + proc.stderr)[-4000:],
    )


def verify(
    repo_name: str,
    task_id: str,
    worktree: Path,
    base_sha: str,
    write_set: list[str],
    commands: list[str],
    timeout: int = 900,
) -> VerificationReport:
    """Snapshot, run, re-snapshot, audit. Report everything; delete nothing."""
    report = VerificationReport(repo=repo_name, task_id=task_id, base_sha=base_sha)

    before = set(gitio.changed_paths(worktree, base_sha, IGNORED_PREFIXES))
    for command in commands:
        report.commands.append(_run(command, worktree, timeout))
    after = set(gitio.changed_paths(worktree, base_sha, IGNORED_PREFIXES))

    changes, refusals = audit_write_set(worktree, base_sha, write_set)
    report.changed_paths = sorted({p for change in changes for p in change.paths})
    report.write_set_refusals = refusals

    for path in sorted(after - before):
        if _covered(path, write_set):
            continue
        report.side_effects.append(
            Refusal(
                Reason.VERIFICATION_SIDE_EFFECT,
                f"{path}: created by a verification command, outside the write_set. "
                "Left in place for inspection — add it to .gitignore or the write_set.",
                {"path": path},
            )
        )
    # A side effect is also a write-set violation; report it once, in its own bucket.
    side_effect_paths = {refusal.detail["path"] for refusal in report.side_effects}
    report.write_set_refusals = [
        refusal
        for refusal in report.write_set_refusals
        if refusal.detail.get("path") not in side_effect_paths
    ]
    return report
