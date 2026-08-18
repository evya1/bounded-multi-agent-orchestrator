"""The production dispatcher: one workflow stage -> one real Pi worker.

This is the only place in the codebase that spawns a paid process. It exists as
its own small module so the workflow state machine can be tested exhaustively
against a scripted dispatcher, while everything about spawning, persisting and
billing lives here and is exercised separately.

Order matters and is deliberate:

1. record the dispatch in the run store BEFORE spawning, with the PID as soon
   as there is one — so a crash between spawn and settle is recoverable rather
   than invisible;
2. stream every protocol event to ``events.jsonl`` as it arrives;
3. on settle, persist the semantic result and the usage;
4. record the ledger entry WHATEVER the outcome, because a failed call still
   consumed tokens and hiding that would corrupt the budget;
5. mark the run terminal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from . import budget as budget_mod
from .pi_rpc import (
    Lifecycle,
    PiRpcWorker,
    SubprocessTransport,
    WorkerOutcome,
    build_rpc_argv,
)
from .run_store import RunManifest, RunStatus, RunStore
from .workflow import Dispatch
from .write_guard import EXTENSION_PATH, PathPolicy


@dataclass
class PiDispatcher:
    """Runs one bounded Pi RPC worker per workflow stage, and persists it all."""

    run_store: RunStore
    ledger: budget_mod.Ledger
    workspace: Path
    repo_name: str
    task_id: str
    task_run_id: str
    base_sha: str
    executable: str = "pi"
    provider_extensions: dict[str, str] | None = None
    policy: PathPolicy | None = None
    environ: dict[str, str] | None = None

    def __call__(self, request: Dispatch) -> WorkerOutcome:
        choice = request.choice
        manifest = self._open_manifest(request)

        transport = SubprocessTransport(
            argv=self._argv(request),
            cwd=self.workspace,
            env=self._environment(request),
        )
        worker = PiRpcWorker(
            transport,
            worker_id=request.worker_id,
            role=request.role,
            provider=choice.provider,
            model=choice.model_id or choice.name,
            limits=request.limits,
            paid=choice.paid,
            on_event=lambda event: self.run_store.append_event(self.task_run_id, event),
        )

        manifest.status = str(RunStatus.DISPATCHED)
        manifest.process_state = "spawning"
        self.run_store.save(manifest)
        try:
            outcome = worker.run(request.prompt)
        finally:
            # The PID is only knowable after start; persist it even on a throw so
            # a lost run is recoverable rather than a mystery.
            manifest.pid = transport.pid
            self.run_store.save(manifest)

        self._persist(manifest, request, outcome, transport)
        return outcome

    # -- argv and environment ---------------------------------------------

    def _argv(self, request: Dispatch) -> list[str]:
        choice = request.choice
        privileges = choice.privileges or {}
        extensions: list[str] = []
        # `--no-extensions` disables discovery, so anything the worker legitimately
        # needs must be named. A local provider is registered by an extension:
        # omit it and the model simply will not resolve.
        provider_extension = (self.provider_extensions or {}).get(choice.provider)
        if provider_extension:
            extensions.append(provider_extension)
        if privileges.get("write_set_only"):
            extensions.append(str(EXTENSION_PATH))
        return build_rpc_argv(
            self.executable,
            choice.provider_pi_name,
            choice.model_id or "",
            reasoning=choice.reasoning,
            tools=privileges.get("tools", "none"),
            allow_bash=bool(privileges.get("bash")),
            extensions=extensions,
        )

    def _environment(self, request: Dispatch) -> dict[str, str]:
        privileges = request.choice.privileges or {}
        environment = dict(self.environ or {})
        if privileges.get("write_set_only") and self.policy is not None:
            environment.update(self.policy.environment(allow_shell=bool(privileges.get("bash"))))
        return environment

    # -- persistence -------------------------------------------------------

    def _open_manifest(self, request: Dispatch) -> RunManifest:
        choice = request.choice
        existing = self.run_store.load(self.task_run_id)
        manifest = existing or RunManifest(task_run_id=self.task_run_id)
        manifest.repo = self.repo_name
        manifest.task_id = self.task_id
        manifest.role = request.role
        manifest.worker_id = request.worker_id
        manifest.provider = choice.provider
        manifest.model = choice.model_id or choice.name
        manifest.reasoning = choice.reasoning
        manifest.base_sha = self.base_sha
        manifest.limits = request.limits.as_dict()
        return self.run_store.create(manifest)

    def _persist(
        self,
        manifest: RunManifest,
        request: Dispatch,
        outcome: WorkerOutcome,
        transport: SubprocessTransport,
    ) -> None:
        telemetry = outcome.telemetry
        manifest.lifecycle = outcome.lifecycle
        manifest.stop_reason = outcome.stop_reason
        manifest.process_state = "exited" if not transport.alive() else "unknown"
        manifest.last_event = telemetry.last_event
        manifest.finished_at = telemetry.finished_at
        manifest.wall_seconds = telemetry.wall_seconds
        manifest.model_calls = telemetry.model_calls
        manifest.tool_calls = telemetry.tool_calls
        manifest.prompt_tokens = telemetry.prompt_tokens
        manifest.completion_tokens = telemetry.completion_tokens
        manifest.reasoning_tokens = telemetry.reasoning_tokens
        manifest.cached_tokens = telemetry.cached_tokens
        manifest.cost_usd = telemetry.cost_usd
        manifest.cost_source = telemetry.cost_source

        manifest.status = str(_status_for(outcome))
        self.run_store.write_artifact(self.task_run_id, "usage.json", telemetry.as_dict())
        if outcome.final_text is not None:
            self.run_store.write_artifact(
                self.task_run_id, f"{request.stage}-final-text.md", outcome.final_text
            )
        self.run_store.save(manifest)
        self._bill(request, outcome)

    def _bill(self, request: Dispatch, outcome: WorkerOutcome) -> None:
        """Record spend whatever happened. A failed call still cost tokens."""
        choice = request.choice
        telemetry = outcome.telemetry
        if choice.paid:
            if telemetry.cost_source == "provider_reported":
                cost, source = telemetry.cost_usd, budget_mod.CostSource.REPORTED
            else:
                cost = budget_mod.estimate_cost(
                    choice.price, telemetry.prompt_tokens, telemetry.completion_tokens
                )
                source = budget_mod.CostSource.ESTIMATED
        else:
            # Local inference: zero EXTERNAL API spend. Never a claim that the
            # GPU, the electricity or the machine itself were free.
            cost, source = 0.0, budget_mod.CostSource.FREE_LOCAL
        now = dt.datetime.now(dt.UTC)
        self.ledger.append(
            budget_mod.LedgerEntry(
                at=now.isoformat(),
                day=now.strftime("%Y-%m-%d"),
                repo=self.repo_name,
                task_id=self.task_id,
                stage=request.stage,
                role=request.role,
                provider=choice.provider,
                model=choice.model_id or choice.name,
                paid=choice.paid,
                input_tokens=telemetry.prompt_tokens,
                output_tokens=telemetry.completion_tokens,
                cache_read_tokens=telemetry.cached_tokens,
                cache_write_tokens=telemetry.cache_write_tokens,
                reasoning_tokens=telemetry.reasoning_tokens,
                cost_usd=float(cost or 0.0),
                cost_source=str(source),
                duration_s=telemetry.wall_seconds,
                exit_code=outcome.exit_code or 0,
                task_run_id=self.task_run_id,
                worker_id=request.worker_id,
                note=outcome.stop_reason,
            )
        )


def _status_for(outcome: WorkerOutcome) -> RunStatus:
    return {
        str(Lifecycle.COMPLETE): RunStatus.COMPLETE,
        str(Lifecycle.BUDGET_EXCEEDED): RunStatus.BUDGET_EXCEEDED,
        str(Lifecycle.TIMED_OUT): RunStatus.TIMED_OUT,
        str(Lifecycle.ABORTED): RunStatus.ABORTED,
        str(Lifecycle.PROCESS_LOST): RunStatus.PROCESS_LOST,
        str(Lifecycle.FAILED): RunStatus.FAILED,
    }.get(outcome.lifecycle, RunStatus.INTERRUPTED)


def should_dispatch(run_store: RunStore, task_run_id: str) -> tuple[bool, str]:
    """Duplicate-dispatch protection. The persisted run is the authority.

    Losing a process handle — because a terminal closed, an editor restarted, or
    this Python process is simply new — is NOT evidence that work needs redoing.
    """
    manifest, verdict = run_store.recover(task_run_id)
    if verdict in ("ALREADY_COMPLETE", "RECOVERED_COMPLETE"):
        return False, f"{verdict}: the worker already settled and its result is persisted"
    if verdict.startswith("ALREADY_FINISHED"):
        return False, f"{verdict}: this run reached a terminal state"
    if verdict == "STILL_RUNNING":
        return False, f"a worker with pid {manifest.pid if manifest else '?'} is still alive"
    if verdict == "PROCESS_LOST":
        return False, (
            "PROCESS_LOST: the previous worker vanished before settling. A human decides "
            "whether to re-dispatch, because part of the work may already have happened."
        )
    return True, verdict
