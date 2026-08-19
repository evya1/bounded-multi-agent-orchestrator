"""Pi RPC process supervision.

Python owns the worker process. Not a shell, not ``nohup``, not a terminal
session, not an editor tab, and not another model: a Python object holds the
subprocess handle, reads its protocol stream, decides when it is finished, and
closes it.

Completion is a PROTOCOL fact
-----------------------------
Verified against the installed Pi 0.84.2 ``docs/rpc.md`` and a live local run:

* ``agent_end`` — "one low-level agent run completes (may still be followed by
  retry, compaction, or queued continuations)". It carries ``willRetry``. It is
  therefore NOT terminal;
* ``agent_settled`` — "the agent run is fully settled; no automatic retry,
  compaction retry, or queued continuation remains". This is the ONLY terminal
  lifecycle event;
* ``message_end`` is one message, not one run;
* stdout silence is not a state at all — it is the absence of evidence, so the
  inactivity watchdog asks ``get_state`` before concluding anything.

Prose is never lifecycle. A worker that types ``=== REVIEW_DONE ===``, and a
prompt that merely *quotes* such a marker, both have exactly zero effect on the
state machine here. A sentinel may carry semantic meaning to a later parser; it
may not end a process.

Framing
-------
``docs/rpc.md`` specifies strict JSONL with LF as the only record delimiter, and
warns that generic line readers which also split on U+2028/U+2029 are not
protocol-compliant. We split on ``\\n`` only and strip one optional trailing
``\\r``.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path


class Lifecycle(StrEnum):
    """Where one worker process is in its life. Derived only from protocol."""

    SPAWNING = "SPAWNING"
    RUNNING = "RUNNING"
    STREAMING = "STREAMING"
    AGENT_END_SEEN = "AGENT_END_SEEN"
    SETTLED = "SETTLED"
    ABORTING = "ABORTING"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    PROCESS_LOST = "PROCESS_LOST"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TIMED_OUT = "TIMED_OUT"
    COMPLETE = "COMPLETE"


#: Lifecycle states from which no further work is dispatched.
TERMINAL = frozenset(
    {
        Lifecycle.COMPLETE,
        Lifecycle.ABORTED,
        Lifecycle.FAILED,
        Lifecycle.PROCESS_LOST,
        Lifecycle.BUDGET_EXCEEDED,
        Lifecycle.TIMED_OUT,
    }
)


class StopReason(StrEnum):
    """Why supervision ended. Machine-readable; drives the retry taxonomy."""

    SETTLED = "SETTLED"
    WALL_DEADLINE = "WALL_DEADLINE"
    INACTIVITY = "INACTIVITY"
    MAX_MODEL_CALLS = "MAX_MODEL_CALLS"
    MAX_TOOL_CALLS = "MAX_TOOL_CALLS"
    HARD_BUDGET = "HARD_BUDGET"
    PROCESS_EXITED = "PROCESS_EXITED"
    SPAWN_FAILED = "SPAWN_FAILED"
    PROMPT_REJECTED = "PROMPT_REJECTED"


@dataclass(frozen=True)
class RoleLimits:
    """Every bound one bounded worker runs under. All of them are enforced.

    ``hard_usd`` is a ceiling on EXTERNAL API spend. A local model's ceiling is
    ``0.0``, which is not a licence to run forever: the turn, tool, wall-clock
    and retry limits still apply, and they are what actually bound a local run.
    """

    max_model_calls: int = 6
    max_tool_calls: int = 12
    wall_seconds: float = 720.0
    inactivity_seconds: float = 180.0
    max_retries: int = 1
    soft_usd: float = 0.0
    hard_usd: float = 0.0

    @classmethod
    def from_config(cls, spec: dict | None) -> RoleLimits:
        spec = spec or {}
        known = {f: spec[f] for f in cls.__dataclass_fields__ if f in spec}
        return cls(**known)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Telemetry:
    """Everything measured about one worker run. No prompts, no credentials."""

    model_calls: int = 0
    tool_calls: int = 0
    turns: int = 0
    agent_runs: int = 0
    auto_retries: int = 0
    compactions: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cost_source: str = "catalog_estimate"
    max_call_cost_usd: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    wall_seconds: float = 0.0
    events_seen: int = 0
    last_event: str = ""
    last_event_at: float = 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["cost_usd"] = round(self.cost_usd, 6)
        data["wall_seconds"] = round(self.wall_seconds, 2)
        return data


@dataclass
class WorkerOutcome:
    """The complete, persisted result of supervising one Pi worker process."""

    worker_id: str
    role: str
    provider: str
    model: str
    lifecycle: str
    stop_reason: str
    settled: bool
    final_text: str | None = None
    telemetry: Telemetry = field(default_factory=Telemetry)
    limits: dict = field(default_factory=dict)
    exit_code: int | None = None
    pid: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Did the run reach protocol completion? Semantics are judged later."""
        return self.lifecycle == str(Lifecycle.COMPLETE)

    def as_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "lifecycle": self.lifecycle,
            "stop_reason": self.stop_reason,
            "settled": self.settled,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "detail": self.detail,
            "limits": self.limits,
            "telemetry": self.telemetry.as_dict(),
            "final_text_chars": len(self.final_text or ""),
        }


# --------------------------------------------------------------- transports


class RpcTransport:
    """A bidirectional JSONL channel to one Pi worker.

    Abstracted so the supervisor's state machine can be tested against exact
    event sequences without a real model, while production uses a real child
    process. The state machine is identical either way.
    """

    def start(self) -> None:
        raise NotImplementedError

    def send(self, command: dict) -> None:
        raise NotImplementedError

    def readline(self, timeout: float) -> str | None:
        """One record, or ``None`` if ``timeout`` elapsed with nothing to read."""
        raise NotImplementedError

    def alive(self) -> bool:
        raise NotImplementedError

    @property
    def pid(self) -> int | None:
        return None

    @property
    def exit_code(self) -> int | None:
        return None

    def close(self, grace: float = 10.0) -> int | None:
        raise NotImplementedError


class SubprocessTransport(RpcTransport):
    """``pi --mode rpc`` as a directly-owned Python child process."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
        self.argv = list(argv)
        self.cwd = Path(cwd)
        self.env = env
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        environment = dict(os.environ)
        if self.env:
            environment.update(self.env)
        self._proc = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        """Strict JSONL: split on LF only, tolerate one trailing CR.

        ``readline()`` on the BINARY stream is deliberate. It splits on ``b"\n"``
        and nothing else, which is exactly the framing ``docs/rpc.md`` specifies;
        a text-mode reader would also split on U+2028/U+2029, which are legal
        inside JSON strings. ``read(n)`` is equally wrong here for a different
        reason: on a buffered pipe it blocks until n bytes arrive or the child
        exits, so a live worker's events would not surface until it died.
        """
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                record = self._proc.stdout.readline()
                if not record:
                    break
                record = record.removesuffix(b"\n").removesuffix(b"\r")
                if record.strip():
                    self._lines.put(record.decode("utf-8", "replace"))
        except (ValueError, OSError):
            pass
        finally:
            self._lines.put(None)  # end-of-stream sentinel

    def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            for line in self._proc.stderr:
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
                    del self._stderr[:-50]
        except (ValueError, OSError):
            pass

    def send(self, command: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise BrokenPipeError("transport is not started")
        payload = (json.dumps(command) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

    def readline(self, timeout: float) -> str | None:
        try:
            item = self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is None:
            self._lines.put(None)  # stay closed for every later reader
            return ""  # empty string == stream closed, distinct from timeout
        return item

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def exit_code(self) -> int | None:
        return self._proc.poll() if self._proc else None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr[-20:])

    def close(self, grace: float = 10.0) -> int | None:
        if self._proc is None:
            return None
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            return self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
        try:
            return self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            return self._proc.wait(timeout=grace)


class ScriptedTransport(RpcTransport):
    """A deterministic fake Pi for tests. Never spawns anything, never spends.

    ``script`` is a list of events (dicts) or the string ``"TIMEOUT"``, which
    makes one ``readline`` return nothing so an inactivity path can be exercised
    without a real clock. ``responses`` maps command names to response payloads.
    """

    def __init__(
        self,
        script: list,
        responses: dict[str, dict] | None = None,
        exit_code: int | None = 0,
    ) -> None:
        self.script = list(script)
        self.responses = responses or {}
        self.sent: list[dict] = []
        self._pending: list[str] = []
        self._closed = False
        self._exit_code = exit_code
        self.started = False

    def start(self) -> None:
        self.started = True

    def send(self, command: dict) -> None:
        self.sent.append(command)
        name = command.get("type", "")
        if name in self.responses:
            payload = {"type": "response", "command": name, "success": True, **self.responses[name]}
            if "id" in command:
                payload["id"] = command["id"]
            self._pending.append(json.dumps(payload))

    def readline(self, timeout: float) -> str | None:
        if self._pending:
            return self._pending.pop(0)
        while self.script:
            item = self.script.pop(0)
            if item == "TIMEOUT":
                return None
            if item == "EOF":
                self._closed = True
                return ""
            return json.dumps(item) if isinstance(item, dict) else str(item)
        self._closed = True
        return ""

    def alive(self) -> bool:
        return self.started and not self._closed

    @property
    def pid(self) -> int | None:
        return -1 if self.started else None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code if self._closed else None

    def close(self, grace: float = 10.0) -> int | None:
        self._closed = True
        return self._exit_code


# ---------------------------------------------------------------- supervisor


def _usage_of(event: dict) -> dict:
    usage = event.get("usage")
    if isinstance(usage, dict):
        return usage
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]
    return {}


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


class PiRpcWorker:
    """Supervises exactly one bounded Pi RPC worker from spawn to close.

    The public contract is small on purpose: ``run(prompt)`` returns a
    ``WorkerOutcome`` and leaves no process behind, whatever happened.
    """

    def __init__(
        self,
        transport: RpcTransport,
        *,
        worker_id: str,
        role: str,
        provider: str,
        model: str,
        limits: RoleLimits,
        paid: bool,
        on_event: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], str] | None = None,
    ) -> None:
        self.transport = transport
        self.worker_id = worker_id
        self.role = role
        self.provider = provider
        self.model = model
        self.limits = limits
        self.paid = paid
        self.on_event = on_event or (lambda event: None)
        self.clock = clock
        self._wallclock = wallclock or _utc_now
        self.state = Lifecycle.SPAWNING
        self.telemetry = Telemetry()
        self._abort_sent = False
        self._stop: StopReason | None = None
        self._detail = ""

    # -- lifecycle rules ---------------------------------------------------

    def _record(self, event: dict, now: float) -> None:
        """Fold one protocol event into lifecycle state and telemetry.

        This method is the whole completion policy. Read it as the answer to
        "when is a worker done?": only ``agent_settled``.
        """
        kind = str(event.get("type", ""))
        self.telemetry.events_seen += 1
        self.telemetry.last_event = kind
        # EVERY protocol event is activity, including tool output and streaming
        # deltas. A worker running a long tool call is working, not stuck.
        self.telemetry.last_event_at = now

        if kind == "agent_start":
            self.state = Lifecycle.RUNNING
        elif kind == "message_start":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                # One assistant message == one paid generation.
                self.telemetry.model_calls += 1
                self.state = Lifecycle.STREAMING
        elif kind == "message_update":
            self.state = Lifecycle.STREAMING
        elif kind == "message_end":
            # A message completing is NOT a run completing. Usage only.
            self._absorb_usage(_usage_of(event))
        elif kind == "turn_end":
            self.telemetry.turns += 1
            self._absorb_usage(_usage_of(event))
        elif kind == "tool_execution_start":
            self.telemetry.tool_calls += 1
        elif kind == "auto_retry_start":
            self.telemetry.auto_retries += 1
            self.state = Lifecycle.RUNNING
        elif kind == "compaction_start":
            self.telemetry.compactions += 1
            self.state = Lifecycle.RUNNING
        elif kind == "agent_end":
            self.telemetry.agent_runs += 1
            # Explicitly NOT terminal. `willRetry` proves automatic work may
            # still follow, and even without it Pi may still compact or drain a
            # queued follow-up. Only `agent_settled` closes the run.
            self.state = Lifecycle.AGENT_END_SEEN
        elif kind == "agent_settled":
            self.state = Lifecycle.SETTLED

        if isinstance(event.get("usage"), dict) and kind != "message_end":
            self._absorb_usage(event["usage"])

    def _absorb_usage(self, usage: dict) -> None:
        """Prefer provider-reported usage over anything we could guess."""
        if not usage:
            return
        self.telemetry.prompt_tokens = max(self.telemetry.prompt_tokens, _int(usage.get("input")))
        self.telemetry.completion_tokens = max(
            self.telemetry.completion_tokens, _int(usage.get("output"))
        )
        self.telemetry.cached_tokens = max(self.telemetry.cached_tokens, _int(usage.get("cacheRead")))
        self.telemetry.cache_write_tokens = max(
            self.telemetry.cache_write_tokens, _int(usage.get("cacheWrite"))
        )
        # Pi normalises reasoning tokens into `output` for most providers; the
        # separate field is recorded when a provider does report it.
        self.telemetry.reasoning_tokens = max(
            self.telemetry.reasoning_tokens,
            _int(usage.get("reasoning")) or _int(usage.get("reasoningTokens")),
        )
        cost = usage.get("cost")
        total = cost.get("total") if isinstance(cost, dict) else cost
        if isinstance(total, int | float):
            self.telemetry.max_call_cost_usd = max(self.telemetry.max_call_cost_usd, float(total))
            # Pi's cumulative usage already sums the session.
            self.telemetry.cost_usd = max(self.telemetry.cost_usd, float(total))
            self.telemetry.cost_source = "provider_reported" if self.paid else "local_zero"

    # -- bounds ------------------------------------------------------------

    def _breach(self, now: float, deadline: float) -> StopReason | None:
        """Which hard limit, if any, forbids letting this worker continue?

        Evaluated at every protocol boundary, so an abort lands BEFORE the next
        generation is dispatched rather than after it has been billed.
        """
        if now >= deadline:
            return StopReason.WALL_DEADLINE
        if self.telemetry.model_calls > self.limits.max_model_calls:
            return StopReason.MAX_MODEL_CALLS
        if self.telemetry.tool_calls > self.limits.max_tool_calls:
            return StopReason.MAX_TOOL_CALLS
        if self.paid and self._next_generation_would_breach():
            return StopReason.HARD_BUDGET
        return None

    def _next_generation_would_breach(self) -> bool:
        """Could ONE more paid generation take this worker past its hard cap?

        A local worker has ``hard_usd == 0`` and ``paid == False``; it is never
        stopped by this rule, only by turns, tools and wall time.
        """
        if self.limits.hard_usd <= 0:
            return True  # a paid role with no budget may not call at all
        projected = self.telemetry.cost_usd + max(
            self.telemetry.max_call_cost_usd, self.limits.hard_usd * 0.1
        )
        return projected > self.limits.hard_usd

    def soft_budget_exceeded(self) -> bool:
        return bool(self.limits.soft_usd) and self.telemetry.cost_usd > self.limits.soft_usd

    # -- the run -----------------------------------------------------------

    def run(self, prompt: str) -> WorkerOutcome:
        started = self.clock()
        self.telemetry.started_at = self._wallclock()
        self.telemetry.last_event_at = started
        deadline = started + self.limits.wall_seconds
        try:
            self.transport.start()
        except (OSError, ValueError) as exc:
            self.state = Lifecycle.FAILED
            return self._finish(StopReason.SPAWN_FAILED, started, f"spawn failed: {exc}")

        self.state = Lifecycle.RUNNING
        try:
            self.transport.send({"id": "prompt-1", "type": "prompt", "message": prompt})
        except (OSError, ValueError) as exc:
            self.state = Lifecycle.PROCESS_LOST
            return self._finish(StopReason.PROCESS_EXITED, started, f"prompt not delivered: {exc}")

        stop = self._supervise(started, deadline)
        return self._finish(stop, started, self._detail)

    def _supervise(self, started: float, deadline: float) -> StopReason:
        """The event loop. Returns the reason supervision ended."""
        while True:
            now = self.clock()
            remaining_wall = deadline - now
            if remaining_wall <= 0:
                self.state = Lifecycle.TIMED_OUT
                return self._abort(StopReason.WALL_DEADLINE, "hard wall-clock deadline reached")

            quiet_for = now - self.telemetry.last_event_at
            remaining_quiet = self.limits.inactivity_seconds - quiet_for
            if remaining_quiet <= 0:
                # Silence is not a verdict. Ask the protocol what is true.
                if self._still_working():
                    self.telemetry.last_event_at = self.clock()
                    continue
                self.state = Lifecycle.TIMED_OUT
                return self._abort(
                    StopReason.INACTIVITY,
                    f"no protocol event for {quiet_for:.0f}s and get_state reports "
                    "the agent is not streaming",
                )

            line = self.transport.readline(min(remaining_wall, remaining_quiet, 1.0))
            if line is None:
                continue  # a poll timeout, not a fact about the worker
            if line == "":
                # stdout closed: the process is gone.
                if self.state in (Lifecycle.SETTLED, Lifecycle.ABORTING):
                    return StopReason.SETTLED if self.state == Lifecycle.SETTLED else (
                        self._stop or StopReason.PROCESS_EXITED
                    )
                self.state = Lifecycle.PROCESS_LOST
                self._detail = "the Pi process closed its stream before agent_settled"
                return StopReason.PROCESS_EXITED

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-protocol noise on stdout is not a lifecycle fact
            if not isinstance(event, dict):
                continue

            if event.get("type") == "response":
                self._on_response(event)
                continue

            self._record(event, self.clock())
            self.on_event(event)

            if self.state == Lifecycle.SETTLED:
                return StopReason.SETTLED

            breach = self._breach(self.clock(), deadline)
            if breach is not None and not self._abort_sent:
                self.state = (
                    Lifecycle.BUDGET_EXCEEDED
                    if breach == StopReason.HARD_BUDGET
                    else Lifecycle.TIMED_OUT
                    if breach == StopReason.WALL_DEADLINE
                    else Lifecycle.ABORTING
                )
                return self._abort(breach, f"limit reached: {breach}")

    def _on_response(self, event: dict) -> None:
        """Command responses carry data, never lifecycle."""
        if event.get("command") == "get_state":
            data = event.get("data") or {}
            self._last_state_probe = bool(data.get("isStreaming") or data.get("isCompacting"))

    _last_state_probe: bool | None = None

    def _still_working(self) -> bool:
        """Ask Pi, do not guess. ``isStreaming``/``isCompacting`` is the answer."""
        if not self.transport.alive():
            return False
        self._last_state_probe = None
        try:
            self.transport.send({"id": "watchdog-state", "type": "get_state"})
        except (OSError, ValueError):
            return False
        # Drain briefly for the response; any event at all also proves activity.
        for _ in range(50):
            line = self.transport.readline(0.1)
            if line is None:
                continue
            if line == "":
                return False
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "response":
                self._on_response(event)
                if event.get("command") == "get_state":
                    return bool(self._last_state_probe)
                continue
            self._record(event, self.clock())
            self.on_event(event)
            return True
        return False

    def _abort(self, reason: StopReason, detail: str) -> StopReason:
        """Tell Pi to stop, then stop reading. Never leave a worker running."""
        self._detail = detail
        self._stop = reason
        if not self._abort_sent and self.transport.alive():
            self._abort_sent = True
            with contextlib.suppress(OSError, ValueError):
                self.transport.send({"id": "abort-1", "type": "abort"})
        return reason

    # -- results -----------------------------------------------------------

    def _request(self, command: dict, name: str, budget_s: float = 20.0) -> dict | None:
        """One request/response round trip after the run has settled."""
        try:
            self.transport.send(command)
        except (OSError, ValueError):
            return None
        deadline = self.clock() + budget_s
        while self.clock() < deadline:
            line = self.transport.readline(0.2)
            if line is None:
                continue
            if line == "":
                return None
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "response" and event.get("command") == name:
                return event.get("data") if event.get("success") else None
        return None

    def _finish(self, stop: StopReason, started: float, detail: str) -> WorkerOutcome:
        """Retrieve the final result if we settled, then close the process."""
        final_text: str | None = None
        if stop == StopReason.SETTLED and self.state == Lifecycle.SETTLED:
            data = self._request(
                {"id": "final-text", "type": "get_last_assistant_text"}, "get_last_assistant_text"
            )
            if isinstance(data, dict):
                text = data.get("text")
                final_text = text if isinstance(text, str) else None
            stats = self._request(
                {"id": "final-stats", "type": "get_session_stats"}, "get_session_stats"
            )
            if isinstance(stats, dict):
                self._absorb_stats(stats)
            self.state = Lifecycle.COMPLETE
        elif stop == StopReason.HARD_BUDGET:
            self.state = Lifecycle.BUDGET_EXCEEDED
        elif stop in (StopReason.WALL_DEADLINE, StopReason.INACTIVITY):
            self.state = Lifecycle.TIMED_OUT
        elif stop in (StopReason.MAX_MODEL_CALLS, StopReason.MAX_TOOL_CALLS):
            self.state = Lifecycle.ABORTED
        elif stop == StopReason.PROCESS_EXITED and self.state != Lifecycle.PROCESS_LOST:
            self.state = Lifecycle.PROCESS_LOST
        elif stop == StopReason.SPAWN_FAILED:
            self.state = Lifecycle.FAILED

        pid = self.transport.pid
        exit_code = self.transport.close(grace=10.0)
        self.telemetry.wall_seconds = self.clock() - started
        self.telemetry.finished_at = self._wallclock()
        if not self.paid:
            # Local inference: zero EXTERNAL API spend. Not zero GPU cost.
            self.telemetry.cost_usd = 0.0
            self.telemetry.cost_source = "local_zero"
        return WorkerOutcome(
            worker_id=self.worker_id,
            role=self.role,
            provider=self.provider,
            model=self.model,
            lifecycle=str(self.state),
            stop_reason=str(stop),
            settled=stop == StopReason.SETTLED,
            final_text=final_text,
            telemetry=self.telemetry,
            limits=self.limits.as_dict(),
            exit_code=exit_code,
            pid=pid,
            detail=detail or self._detail,
        )

    def _absorb_stats(self, stats: dict) -> None:
        tokens = stats.get("tokens") if isinstance(stats.get("tokens"), dict) else {}
        self.telemetry.prompt_tokens = max(self.telemetry.prompt_tokens, _int(tokens.get("input")))
        self.telemetry.completion_tokens = max(
            self.telemetry.completion_tokens, _int(tokens.get("output"))
        )
        self.telemetry.cached_tokens = max(self.telemetry.cached_tokens, _int(tokens.get("cacheRead")))
        self.telemetry.cache_write_tokens = max(
            self.telemetry.cache_write_tokens, _int(tokens.get("cacheWrite"))
        )
        if isinstance(stats.get("toolCalls"), int):
            self.telemetry.tool_calls = max(self.telemetry.tool_calls, stats["toolCalls"])
        cost = stats.get("cost")
        if isinstance(cost, int | float):
            self.telemetry.cost_usd = max(self.telemetry.cost_usd, float(cost))
            self.telemetry.cost_source = "provider_reported" if self.paid else "local_zero"


def _utc_now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def build_rpc_argv(
    executable: str,
    provider: str,
    model: str,
    *,
    reasoning: str | None = None,
    tools: str | list[str] = "none",
    allow_bash: bool = False,
    extensions: list[str] | None = None,
    no_extension_discovery: bool = True,
) -> list[str]:
    """Argv for one bounded RPC worker.

    Privilege is argv-level, exactly as in the print-mode adapter: a read-only
    role is handed ``--no-tools``, so "the prompt said not to write" never has
    to be trusted. ``--no-extensions`` disables discovery; every extension the
    worker is allowed — the bounded-write guard, the local provider — must be
    named explicitly with ``-e``.
    """
    argv = [executable, "--mode", "rpc", "--no-session", "--provider", provider, "--model", model]
    if reasoning:
        argv += ["--thinking", reasoning]
    if tools == "none":
        argv.append("--no-tools")
    else:
        # The `edit` privilege is the IMPLEMENTER/FIXER role. Its prompt states
        # "your job is to edit; proving the code works is not delegated to you",
        # and the context compiler already includes every write_set file VERBATIM.
        # Granting read/ls/find/grep lets a chatty model burn its whole bounded
        # tool budget re-scouting files it already has (observed: 11 reads, 0
        # edits, MAX_MODEL_CALLS abort). Edit/write only forces direct edits from
        # the verbatim context. If a task needs material not in its packet, the
        # worker must STOP_NEEDS_ORCHESTRATOR (the bounded contract), not scout.
        if tools == "edit":
            allowed = ["edit", "write"]
        elif isinstance(tools, list):
            allowed = list(tools)
        else:
            allowed = ["read", "ls", "find", "grep", "edit", "write"]
        if allow_bash:
            allowed.append("bash")
        argv += ["--tools", ",".join(allowed)]
    if no_extension_discovery:
        argv.append("--no-extensions")
    for extension in extensions or []:
        argv += ["-e", str(extension)]
    return argv


def iter_events(path: Path) -> Iterator[dict]:
    """Replay a persisted ``events.jsonl`` stream."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
