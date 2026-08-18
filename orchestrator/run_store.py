"""Durable run state.

The previous workflow's worst failure was that knowledge of a running worker
lived in a terminal session. Close the tab and the work became invisible;
re-running the command dispatched it a second time.

So run state lives on disk, is written atomically, and is authoritative:

    state/runs/<task_run_id>/
        manifest.json        the run's identity, routing, budgets and status
        events.jsonl         every protocol event, appended as it arrived
        worker-result.json   the validated semantic result
        validation.json      deterministic gate results
        review-packet.json   exactly what the reviewer was shown
        reviewer-result.json the reviewer's structured verdict
        usage.json           tokens, cost and cost provenance

No database. A directory per run is enough, is inspectable with ``cat``, and
survives anything short of losing the disk.

The recovery rule is the point of the whole module:

* manifest says ``settled`` and a result is persisted  -> the worker COMPLETED.
  Never redispatch. The absence of a process handle is not evidence of anything;
* manifest says running, but the recorded PID is gone  -> PROCESS_LOST. That is
  an explicit state a human or a policy resolves, NOT an automatic re-run.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path


class RunStatus(StrEnum):
    """The durable status of one task run."""

    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    SETTLED = "SETTLED"
    COMPLETE = "COMPLETE"
    INTERRUPTED = "INTERRUPTED"
    PROCESS_LOST = "PROCESS_LOST"
    ABORTED = "ABORTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


#: Statuses that mean "this run finished; do not dispatch it again".
FINISHED = frozenset(
    {
        RunStatus.COMPLETE,
        RunStatus.ABORTED,
        RunStatus.BUDGET_EXCEEDED,
        RunStatus.TIMED_OUT,
        RunStatus.FAILED,
    }
)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def new_task_run_id(repo: str, task_id: str, role: str) -> str:
    """A stable, sortable, human-readable run identity."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{repo}-{task_id}-{role}-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass
class RunManifest:
    """Everything needed to reason about a run without re-reading its events."""

    task_run_id: str
    run_id: str = ""
    repo: str = ""
    task_id: str = ""
    role: str = ""
    worker_id: str = ""
    provider: str = ""
    model: str = ""
    reasoning: str | None = None

    base_sha: str = ""
    candidate_sha: str = ""

    status: str = str(RunStatus.CREATED)
    lifecycle: str = ""
    stop_reason: str = ""
    pid: int | None = None
    process_state: str = "none"
    last_event: str = ""
    last_event_at: str = ""

    started_at: str = ""
    finished_at: str = ""
    wall_seconds: float = 0.0

    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    cost_source: str = "catalog_estimate"

    retry_count: int = 0
    limits: dict = field(default_factory=dict)

    writer_result: str = ""
    validation_result: str = ""
    review_result: str = ""
    workflow_state: str = ""

    notes: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return RunStatus(self.status) in FINISHED

    @property
    def completed_successfully(self) -> bool:
        return self.status == str(RunStatus.COMPLETE)

    def as_dict(self) -> dict:
        return asdict(self)


def _atomic_write(path: Path, text: str) -> None:
    """Write-then-rename, so a crash mid-write never truncates run state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def pid_alive(pid: int | None) -> bool:
    """Is this PID still a live process? Best-effort and never fatal.

    A recycled PID could in principle answer yes. That is why a live PID is
    never used to conclude a run SUCCEEDED — only a persisted settle plus a
    persisted result does that.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunStore:
    """Filesystem-backed run persistence. One directory per task run."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- locations ---------------------------------------------------------

    def dir_for(self, task_run_id: str) -> Path:
        return self.root / task_run_id

    def manifest_path(self, task_run_id: str) -> Path:
        return self.dir_for(task_run_id) / "manifest.json"

    def events_path(self, task_run_id: str) -> Path:
        return self.dir_for(task_run_id) / "events.jsonl"

    def artifact_path(self, task_run_id: str, name: str) -> Path:
        return self.dir_for(task_run_id) / name

    def writable(self) -> tuple[bool, str]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".writable-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return False, f"{self.root}: {exc}"
        return True, str(self.root)

    # -- manifests ---------------------------------------------------------

    def create(self, manifest: RunManifest) -> RunManifest:
        manifest.started_at = manifest.started_at or utc_now()
        self.save(manifest)
        return manifest

    def save(self, manifest: RunManifest) -> RunManifest:
        _atomic_write(
            self.manifest_path(manifest.task_run_id),
            json.dumps(manifest.as_dict(), indent=2, sort_keys=True),
        )
        return manifest

    def load(self, task_run_id: str) -> RunManifest | None:
        path = self.manifest_path(task_run_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f: data[f] for f in RunManifest.__dataclass_fields__ if f in data}
        return RunManifest(**known)

    def all_runs(self) -> list[RunManifest]:
        if not self.root.is_dir():
            return []
        runs = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            manifest = self.load(directory.name)
            if manifest is not None:
                runs.append(manifest)
        return runs

    def runs_for(self, repo: str, task_id: str, role: str | None = None) -> list[RunManifest]:
        return [
            manifest
            for manifest in self.all_runs()
            if manifest.repo == repo
            and manifest.task_id == task_id
            and (role is None or manifest.role == role)
        ]

    # -- events ------------------------------------------------------------

    def append_event(self, task_run_id: str, event: dict) -> None:
        """Append one protocol event. Line-buffered; never rewrites history."""
        path = self.events_path(task_run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def write_artifact(self, task_run_id: str, name: str, data: object) -> Path:
        path = self.artifact_path(task_run_id, name)
        text = data if isinstance(data, str) else json.dumps(data, indent=2, sort_keys=True)
        _atomic_write(path, text)
        return path

    def read_artifact(self, task_run_id: str, name: str) -> dict | None:
        path = self.artifact_path(task_run_id, name)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    # -- recovery ----------------------------------------------------------

    def recover(self, task_run_id: str) -> tuple[RunManifest | None, str]:
        """What is true about this run right now? Returns (manifest, verdict).

        The verdict is the ONLY input the dispatcher uses to decide whether a
        worker still needs to run. Losing a process handle is never one of the
        reasons to run one again.
        """
        manifest = self.load(task_run_id)
        if manifest is None:
            return None, "NO_SUCH_RUN"

        if manifest.completed_successfully:
            return manifest, "ALREADY_COMPLETE"
        if manifest.finished:
            return manifest, f"ALREADY_FINISHED:{manifest.status}"

        # Settled with a persisted result is complete, whatever the process did
        # afterwards and whatever this Python process knows about it.
        if manifest.status == str(RunStatus.SETTLED) and self.read_artifact(
            task_run_id, "worker-result.json"
        ):
            manifest.status = str(RunStatus.COMPLETE)
            manifest.notes.append("recovered: settled with a persisted result")
            self.save(manifest)
            return manifest, "RECOVERED_COMPLETE"

        if manifest.status == str(RunStatus.DISPATCHED):
            if pid_alive(manifest.pid):
                return manifest, "STILL_RUNNING"
            manifest.status = str(RunStatus.PROCESS_LOST)
            manifest.process_state = "gone"
            manifest.notes.append(
                f"recovered: dispatched but pid {manifest.pid} is gone and no settle was recorded"
            )
            self.save(manifest)
            return manifest, "PROCESS_LOST"

        return manifest, f"INCOMPLETE:{manifest.status}"

    def latest_for(self, repo: str, task_id: str, role: str) -> RunManifest | None:
        runs = self.runs_for(repo, task_id, role)
        return runs[-1] if runs else None
