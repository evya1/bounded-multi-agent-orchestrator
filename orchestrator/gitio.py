"""Git plumbing.

Every read of repository state goes through here, using NUL-delimited plumbing
output rather than whitespace splitting: task paths can and do contain spaces,
and v3's ``.split()`` silently mangled them.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import OrchestratorError, Reason


class ChangeKind(StrEnum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPE_CHANGED = "T"
    UNTRACKED = "?"


@dataclass(frozen=True, order=True)
class Change:
    """One path-level change, whatever its origin."""

    path: str
    kind: ChangeKind
    origin: str            # committed | staged | worktree | untracked
    old_path: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path this change touches — a rename touches two."""
        return (self.path,) if self.old_path is None else (self.old_path, self.path)


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    """Run a git command and return stdout. Never swallows a failure."""
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    if check and proc.returncode != 0:
        raise OrchestratorError(
            Reason.CONFIG_INVALID,
            f"git {' '.join(args)} failed in {cwd} (exit {proc.returncode}): {proc.stderr.strip()}",
        )
    return proc.stdout


def rev_parse(repo: Path, ref: str) -> str:
    return git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo).strip()


def origin_url(repo: Path) -> str:
    return git(["config", "--get", "remote.origin.url"], repo, check=False).strip()


def current_branch(repo: Path) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], repo).strip()


def is_clean(repo: Path) -> bool:
    return not git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], repo).strip("\0")


def _split_nul(blob: str) -> list[str]:
    return [part for part in blob.split("\0") if part]


def committed_changes(repo: Path, base: str, head: str = "HEAD") -> list[Change]:
    """Changes committed on the task branch relative to its recorded base."""
    raw = git(["diff", "--name-status", "--find-renames", "-z", base, head], repo)
    return _parse_name_status(raw, origin="committed")


def _parse_name_status(raw: str, origin: str) -> list[Change]:
    """Parse ``--name-status -z`` output; rename/copy records span three fields."""
    fields = _split_nul(raw)
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        letter = status[0]
        if letter in ("R", "C"):
            old, new = fields[index + 1], fields[index + 2]
            changes.append(Change(new, ChangeKind(letter), origin, old_path=old))
            index += 3
        else:
            changes.append(Change(fields[index + 1], ChangeKind(letter), origin))
            index += 2
    return changes


def worktree_changes(repo: Path) -> list[Change]:
    """Staged, unstaged and untracked changes, including deletions and renames."""
    raw = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], repo)
    fields = raw.split("\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        if not entry:
            index += 1
            continue
        index_status, tree_status, path = entry[0], entry[1], entry[3:]
        if index_status == "?" and tree_status == "?":
            changes.append(Change(path, ChangeKind.UNTRACKED, "untracked"))
            index += 1
            continue
        old_path = None
        if index_status in ("R", "C"):
            # porcelain v1 -z emits the ORIGINAL path as the following NUL field.
            index += 1
            old_path = fields[index] if index < len(fields) else None
        if index_status not in (" ", "?"):
            changes.append(Change(path, ChangeKind(index_status), "staged", old_path=old_path))
        if tree_status not in (" ", "?"):
            changes.append(Change(path, ChangeKind(tree_status), "worktree", old_path=old_path))
        index += 1
    return changes


def all_changes(repo: Path, base: str, ignore_prefixes: tuple[str, ...] = ()) -> list[Change]:
    """Committed + staged + unstaged + untracked, deletions and renames included."""
    changes = committed_changes(repo, base) + worktree_changes(repo)
    if ignore_prefixes:
        changes = [
            change
            for change in changes
            if not any(p.startswith(ignore_prefixes) for p in change.paths)
        ]
    return sorted(set(changes))


def changed_paths(repo: Path, base: str, ignore_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Every path touched, from any origin. A rename contributes both sides."""
    paths: set[str] = set()
    for change in all_changes(repo, base, ignore_prefixes):
        paths.update(change.paths)
    return sorted(paths)


def _entry_signature(repo: Path, path: str) -> tuple[str, str]:
    """The Git-visible mode and content digest of one path.

    Mode is included because an executable-bit flip or a file/symlink swap is a
    real, reviewable change that leaves file *content* byte-identical. Hashing
    content alone would let such a mutation survive an existing approval.
    """
    target = repo / path
    try:
        info = target.lstat()
    except (OSError, ValueError):
        return "gone", "DELETED"
    if stat.S_ISLNK(info.st_mode):
        # Hash the link target, not the file it points at.
        return "120000", hashlib.sha256(os.readlink(target).encode()).hexdigest()
    if stat.S_ISDIR(info.st_mode):
        return "040000", "DIRECTORY"
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    return mode, hashlib.sha256(target.read_bytes()).hexdigest()


def change_manifest(repo: Path, base: str, ignore_prefixes: tuple[str, ...] = ()) -> str:
    """A canonical, diff-format-independent description of the change set.

    One sorted ``path<TAB>mode<TAB>digest`` line per touched path. Hashing
    *this* rather than ``git diff`` output means an approval survives a git
    version bump but never survives an actual content change, a mode change, a
    symlink swap, a deletion, or a rename.
    """
    lines = []
    for path in changed_paths(repo, base, ignore_prefixes):
        mode, digest = _entry_signature(repo, path)
        lines.append(f"{path}\t{mode}\t{digest}")
    return "\n".join(lines) + ("\n" if lines else "")


#: Untracked files larger than this are summarised rather than inlined.
MAX_INLINE_BYTES = 200_000


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8000]


def review_patch(repo: Path, base: str, ignore_prefixes: tuple[str, ...] = ()) -> str:
    """The actual reviewable change: a unified diff plus new-file contents.

    A reviewer cannot review code from hashes. This produces the real patch for
    tracked changes (committed, staged and unstaged alike, with renames and
    deletions detected) and inlines the content of newly created untracked text
    files. It is scoped strictly to the task's own change set — no unrelated
    repository content is included.
    """
    changes = all_changes(repo, base, ignore_prefixes)
    if not changes:
        return "(no changes against the recorded base)"

    sections: list[str] = []
    tracked = sorted({p for c in changes if c.origin != "untracked" for p in c.paths})
    if tracked:
        diff = git(
            ["diff", "--no-color", "--no-ext-diff", "--find-renames", "--", *tracked],
            repo,
            check=False,
        )
        committed = git(
            ["diff", "--no-color", "--no-ext-diff", "--find-renames", base, "HEAD", "--", *tracked],
            repo,
            check=False,
        )
        staged = git(
            ["diff", "--no-color", "--no-ext-diff", "--find-renames", "--cached", "--", *tracked],
            repo,
            check=False,
        )
        body = "\n".join(part for part in (committed, staged, diff) if part.strip())
        if body.strip():
            sections.append("===== UNIFIED DIFF vs base =====\n" + body.rstrip())

    untracked = sorted({c.path for c in changes if c.origin == "untracked"})
    for path in untracked:
        target = repo / path
        if not target.is_file():
            continue
        data = target.read_bytes()
        header = f"===== NEW FILE (untracked): {path} ====="
        if _looks_binary(data):
            sections.append(f"{header}\n<binary, {len(data)} bytes — not shown>")
        elif len(data) > MAX_INLINE_BYTES:
            sections.append(
                f"{header}\n<{len(data)} bytes, truncated to {MAX_INLINE_BYTES}>\n"
                + data[:MAX_INLINE_BYTES].decode("utf-8", errors="replace")
            )
        else:
            sections.append(f"{header}\n{data.decode('utf-8', errors='replace')}")

    structural = [
        f"  {c.kind.name:<12} {c.origin:<10} "
        + (f"{c.old_path} -> {c.path}" if c.old_path else c.path)
        for c in changes
    ]
    sections.append("===== CHANGE SUMMARY (kind / origin / path) =====\n" + "\n".join(structural))
    return "\n\n".join(sections)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
