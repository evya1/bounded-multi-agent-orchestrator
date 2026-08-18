"""Run state must survive losing the UI, the terminal, or this process.

CASE G in the failure catalogue: Python restarts after a worker already
settled. The correct behaviour is to recover the result, not to pay for the work
a second time.
"""

from __future__ import annotations

import json
import os

import pytest

from orchestrator.dispatch import should_dispatch
from orchestrator.run_store import (
    RunManifest,
    RunStatus,
    RunStore,
    new_task_run_id,
    pid_alive,
)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "state" / "runs")


def _dispatched(store: RunStore, pid: int | None) -> RunManifest:
    manifest = store.create(
        RunManifest(
            task_run_id=new_task_run_id("police", "T002", "writer"),
            repo="police",
            task_id="T002",
            role="writer",
        )
    )
    manifest.status = str(RunStatus.DISPATCHED)
    manifest.pid = pid
    return store.save(manifest)


def test_a_settled_run_with_a_persisted_result_is_never_redispatched(store):
    """CASE G — the authority is the persisted run, not a live process handle."""
    manifest = _dispatched(store, pid=os.getpid())
    manifest.status = str(RunStatus.SETTLED)
    store.save(manifest)
    store.write_artifact(manifest.task_run_id, "worker-result.json", {"status": "done"})

    # A brand-new store object: exactly what a restarted process sees.
    fresh = RunStore(store.root)
    allowed, why = should_dispatch(fresh, manifest.task_run_id)

    assert allowed is False
    assert "settled" in why
    assert fresh.load(manifest.task_run_id).status == str(RunStatus.COMPLETE)


def test_a_completed_run_is_never_redispatched(store):
    manifest = _dispatched(store, pid=None)
    manifest.status = str(RunStatus.COMPLETE)
    store.save(manifest)

    allowed, why = should_dispatch(RunStore(store.root), manifest.task_run_id)

    assert allowed is False
    assert "ALREADY_COMPLETE" in why


def test_a_vanished_process_becomes_process_lost_not_a_silent_rerun(store):
    """A dead PID with no settle is an explicit state a human resolves."""
    manifest = _dispatched(store, pid=4_000_000)  # far above any live pid
    assert pid_alive(4_000_000) is False

    allowed, why = should_dispatch(RunStore(store.root), manifest.task_run_id)

    assert allowed is False
    assert "PROCESS_LOST" in why
    assert store.load(manifest.task_run_id).status == str(RunStatus.PROCESS_LOST)


def test_a_live_process_is_not_redispatched(store):
    manifest = _dispatched(store, pid=os.getpid())

    allowed, why = should_dispatch(RunStore(store.root), manifest.task_run_id)

    assert allowed is False
    assert "still alive" in why


def test_a_fresh_run_is_dispatchable(store):
    manifest = store.create(
        RunManifest(task_run_id=new_task_run_id("police", "T002", "writer"), repo="police")
    )

    allowed, _ = should_dispatch(store, manifest.task_run_id)

    assert allowed is True


def test_budget_exceeded_is_terminal_and_not_retried(store):
    manifest = _dispatched(store, pid=None)
    manifest.status = str(RunStatus.BUDGET_EXCEEDED)
    store.save(manifest)

    allowed, why = should_dispatch(RunStore(store.root), manifest.task_run_id)

    assert allowed is False
    assert "BUDGET_EXCEEDED" in why


def test_manifests_are_written_atomically(store, monkeypatch):
    """A crash mid-write must not leave a truncated manifest behind."""
    manifest = store.create(RunManifest(task_run_id="atomic-1", repo="police"))
    original = store.manifest_path("atomic-1").read_text()

    import orchestrator.run_store as run_store_mod

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(run_store_mod.os, "replace", explode)
    manifest.cost_usd = 999.0
    with pytest.raises(OSError):
        store.save(manifest)

    # The old file is intact and still parses.
    assert store.manifest_path("atomic-1").read_text() == original
    assert json.loads(original)["cost_usd"] == 0.0
    # No temporary file was left lying around.
    assert not list(store.dir_for("atomic-1").glob("*.tmp"))


def test_events_are_appended_and_replayable(store):
    store.append_event("run-1", {"type": "agent_start"})
    store.append_event("run-1", {"type": "agent_settled"})

    lines = store.events_path("run-1").read_text().strip().splitlines()

    assert [json.loads(line)["type"] for line in lines] == ["agent_start", "agent_settled"]


def test_the_run_store_reports_whether_it_is_writable(store):
    ok, detail = store.writable()

    assert ok is True
    assert str(store.root) in detail
