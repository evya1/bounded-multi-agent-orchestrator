"""Lifecycle regression tests.

Each test here corresponds to a way the previous Claude-supervised workflow got
completion wrong and paid for it. They are written as assertions about the
PROTOCOL, because that is the only thing the new supervisor is allowed to read.

None of these call a model, spawn a GPU, touch OpenRouter or spend anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from orchestrator.pi_rpc import (
    Lifecycle,
    PiRpcWorker,
    RoleLimits,
    ScriptedTransport,
    StopReason,
    SubprocessTransport,
    build_rpc_argv,
)

FAKE_PI = Path(__file__).parent / "fake_pi.py"

SENTINEL = "=== REVIEW_DONE ==="

STANDARD_RESPONSES = {
    "get_last_assistant_text": {"data": {"text": "final answer"}},
    "get_session_stats": {"data": {"tokens": {"input": 500, "output": 40}, "cost": 0.0}},
    "get_state": {"data": {"isStreaming": False}},
}


def _worker(script, responses=None, limits=None, paid=False, clock=None):
    transport = ScriptedTransport(script, {**STANDARD_RESPONSES, **(responses or {})})
    return PiRpcWorker(
        transport,
        worker_id="w1",
        role="writer",
        provider="local_llama",
        model="qwen3.8-27b",
        limits=limits or RoleLimits(wall_seconds=60, inactivity_seconds=30),
        paid=paid,
        clock=clock or (lambda: 0.0),
    ), transport


def assistant(text: str) -> dict:
    return {
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


# ------------------------------------------------------------------ CASE A/B


def test_prompt_containing_a_sentinel_does_not_end_the_run():
    """CASE A — the PROMPT quotes a completion marker. It must mean nothing."""
    worker, transport = _worker(
        [{"type": "agent_start"}, assistant("ack"), {"type": "agent_end", "willRetry": False},
         {"type": "agent_settled"}]
    )
    outcome = worker.run(f"Follow this contract and finish with {SENTINEL} when done.")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.stop_reason == str(StopReason.SETTLED)
    # The prompt went out verbatim; the supervisor simply never consulted it.
    sent = [command for command in transport.sent if command["type"] == "prompt"]
    assert SENTINEL in sent[0]["message"]


def test_assistant_text_containing_a_sentinel_early_does_not_end_the_run():
    """CASE B — the model emits the marker mid-run. Still not terminal."""
    worker, _ = _worker(
        [
            {"type": "agent_start"},
            assistant(f"{SENTINEL}\nactually I am still working"),
            {"type": "tool_execution_start", "toolName": "read", "toolCallId": "t1"},
            {"type": "tool_execution_end", "toolCallId": "t1", "isError": False},
            assistant("now I am really done"),
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
    )
    outcome = worker.run("do the work")

    assert outcome.settled is True
    assert outcome.telemetry.tool_calls == 1
    # The sentinel appeared at event 2 of 7; every later event was still processed.
    assert outcome.telemetry.events_seen == 7


def test_a_sentinel_alone_never_settles_a_run():
    """A stream that ONLY contains the marker must not reach COMPLETE."""
    worker, _ = _worker([{"type": "agent_start"}, assistant(SENTINEL), "EOF"])
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.PROCESS_LOST)
    assert outcome.settled is False


# -------------------------------------------------------------------- CASE C


def test_message_end_is_not_terminal():
    """CASE C — a completed message is one message, not a completed run."""
    worker, _ = _worker([{"type": "agent_start"}, assistant("here is my answer"), "EOF"])
    outcome = worker.run("go")

    assert outcome.lifecycle != str(Lifecycle.COMPLETE)
    assert outcome.settled is False


def test_turn_end_is_not_terminal():
    worker, _ = _worker(
        [{"type": "agent_start"}, {"type": "turn_end", "message": {}, "toolResults": []}, "EOF"]
    )
    outcome = worker.run("go")

    assert outcome.settled is False
    assert outcome.telemetry.turns == 1


# -------------------------------------------------------------------- CASE D


def test_agent_end_with_retry_pending_is_still_active():
    """CASE D — `agent_end` carries willRetry. The run is NOT over."""
    worker, _ = _worker(
        [
            {"type": "agent_start"},
            {"type": "agent_end", "willRetry": True},
            {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3},
            {"type": "auto_retry_end", "success": True, "attempt": 2},
            assistant("recovered"),
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.telemetry.agent_runs == 2
    assert outcome.telemetry.auto_retries == 1


def test_agent_end_alone_is_not_terminal_even_without_retry():
    """`willRetry: false` still permits compaction or a queued follow-up."""
    worker, _ = _worker([{"type": "agent_start"}, {"type": "agent_end", "willRetry": False}, "TIMEOUT", "EOF"])
    outcome = worker.run("go")

    assert outcome.settled is False
    assert outcome.lifecycle == str(Lifecycle.PROCESS_LOST)


def test_agent_end_followed_by_compaction_then_settle():
    worker, _ = _worker(
        [
            {"type": "agent_start"},
            {"type": "agent_end", "willRetry": False},
            {"type": "compaction_start", "reason": "overflow"},
            {"type": "compaction_end", "reason": "overflow", "willRetry": True, "aborted": False},
            assistant("continued after compaction"),
            {"type": "agent_end", "willRetry": False},
            {"type": "agent_settled"},
        ]
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.telemetry.compactions == 1


# -------------------------------------------------------------------- CASE E


def test_agent_settled_is_terminal_and_result_is_retrieved_exactly_once():
    """CASE E — settle, then retrieve the final text with ONE request."""
    worker, transport = _worker(
        [{"type": "agent_start"}, assistant("draft"), {"type": "agent_end", "willRetry": False},
         {"type": "agent_settled"}]
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.final_text == "final answer"
    requests = [c["type"] for c in transport.sent if c["type"] == "get_last_assistant_text"]
    assert requests == ["get_last_assistant_text"]


def test_no_result_is_retrieved_when_the_run_did_not_settle():
    worker, transport = _worker([{"type": "agent_start"}, "EOF"])
    outcome = worker.run("go")

    assert outcome.final_text is None
    assert "get_last_assistant_text" not in [c["type"] for c in transport.sent]


# -------------------------------------------------------------------- CASE F


def test_a_quiet_worker_is_not_killed_when_the_protocol_says_it_is_streaming():
    """CASE F — silence is not a verdict. Ask `get_state` first."""
    clock = iter([0.0] + [float(n) for n in range(1, 400)])
    transport = ScriptedTransport(
        ["TIMEOUT"] * 40 + [{"type": "agent_settled"}],
        {**STANDARD_RESPONSES, "get_state": {"data": {"isStreaming": True}}},
    )
    worker = PiRpcWorker(
        transport,
        worker_id="w1", role="writer", provider="local_llama", model="qwen3.8-27b",
        limits=RoleLimits(wall_seconds=10_000, inactivity_seconds=5),
        paid=False,
        clock=lambda: next(clock),
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert any(command["type"] == "get_state" for command in transport.sent)


def test_a_quiet_worker_that_is_not_streaming_times_out():
    clock = iter([0.0] + [float(n) for n in range(1, 400)])
    transport = ScriptedTransport(
        ["TIMEOUT"] * 60,
        {**STANDARD_RESPONSES, "get_state": {"data": {"isStreaming": False}}},
    )
    worker = PiRpcWorker(
        transport,
        worker_id="w1", role="writer", provider="local_llama", model="qwen3.8-27b",
        limits=RoleLimits(wall_seconds=10_000, inactivity_seconds=5),
        paid=False,
        clock=lambda: next(clock),
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.TIMED_OUT)
    assert outcome.stop_reason == str(StopReason.INACTIVITY)
    assert any(command["type"] == "abort" for command in transport.sent)


def test_the_inactivity_watchdog_treats_tool_output_as_activity():
    """A long tool call produces no prose. It must not look like a hang."""
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0] + [float(n) for n in range(5, 400)])
    transport = ScriptedTransport(
        [
            {"type": "agent_start"},
            {"type": "tool_execution_start", "toolName": "bash", "toolCallId": "t1"},
            {"type": "tool_execution_update", "toolCallId": "t1", "partialResult": {}},
            {"type": "tool_execution_update", "toolCallId": "t1", "partialResult": {}},
            {"type": "tool_execution_end", "toolCallId": "t1", "isError": False},
            {"type": "agent_settled"},
        ],
        STANDARD_RESPONSES,
    )
    worker = PiRpcWorker(
        transport,
        worker_id="w1", role="writer", provider="local_llama", model="qwen3.8-27b",
        limits=RoleLimits(wall_seconds=10_000, inactivity_seconds=3),
        paid=False,
        clock=lambda: next(ticks),
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert "get_state" not in [command["type"] for command in transport.sent]


# ------------------------------------------------------------- hard limits


def test_hard_wall_deadline_aborts_and_persists_the_timeout():
    clock = iter([0.0] + [float(n) * 10 for n in range(1, 400)])
    transport = ScriptedTransport(["TIMEOUT"] * 60, STANDARD_RESPONSES)
    worker = PiRpcWorker(
        transport,
        worker_id="w1", role="writer", provider="openrouter", model="x",
        limits=RoleLimits(wall_seconds=25, inactivity_seconds=10_000),
        paid=True,
        clock=lambda: next(clock),
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.TIMED_OUT)
    assert outcome.stop_reason == str(StopReason.WALL_DEADLINE)
    assert any(command["type"] == "abort" for command in transport.sent)


def test_max_model_calls_aborts_before_another_generation():
    events = [{"type": "agent_start"}]
    for index in range(5):
        events.append({"type": "message_start", "message": {"role": "assistant"}})
        events.append(assistant(f"turn {index}"))
    events.append({"type": "agent_settled"})
    worker, _transport = _worker(events, limits=RoleLimits(max_model_calls=2, wall_seconds=1000))
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.ABORTED)
    assert outcome.stop_reason == str(StopReason.MAX_MODEL_CALLS)
    assert outcome.telemetry.model_calls == 3  # the one that crossed the line, then abort


def test_max_tool_calls_aborts():
    events = [{"type": "agent_start"}]
    for index in range(6):
        events.append({"type": "tool_execution_start", "toolName": "read", "toolCallId": str(index)})
    events.append({"type": "agent_settled"})
    worker, transport = _worker(events, limits=RoleLimits(max_tool_calls=2, wall_seconds=1000))
    outcome = worker.run("go")

    assert outcome.stop_reason == str(StopReason.MAX_TOOL_CALLS)
    assert any(command["type"] == "abort" for command in transport.sent)


def test_hard_cost_budget_aborts_before_the_next_paid_generation():
    """CASE I — the reviewer reaches its cost ceiling. No second expensive run."""
    usage = {"input": 50_000, "output": 8000, "cost": {"total": 0.14}}
    worker, transport = _worker(
        [
            {"type": "agent_start"},
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "message_end", "message": {"role": "assistant", "usage": usage}},
            {"type": "message_start", "message": {"role": "assistant"}},
            assistant("this second call should never be reached"),
            {"type": "agent_settled"},
        ],
        limits=RoleLimits(max_model_calls=9, wall_seconds=1000, hard_usd=0.15),
        paid=True,
    )
    outcome = worker.run("review this")

    assert outcome.lifecycle == str(Lifecycle.BUDGET_EXCEEDED)
    assert outcome.stop_reason == str(StopReason.HARD_BUDGET)
    assert any(command["type"] == "abort" for command in transport.sent)


def test_a_paid_role_with_no_budget_may_not_call_at_all():
    worker, _transport = _worker(
        [{"type": "agent_start"}, assistant("hi"), {"type": "agent_settled"}],
        limits=RoleLimits(hard_usd=0.0, wall_seconds=1000),
        paid=True,
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.BUDGET_EXCEEDED)


def test_a_local_worker_is_never_stopped_by_a_cost_budget():
    """A local model has hard_usd 0.0 and is bounded by turns and time instead."""
    worker, _ = _worker(
        [{"type": "agent_start"}, assistant("hi"), {"type": "agent_settled"}],
        limits=RoleLimits(hard_usd=0.0, wall_seconds=1000),
        paid=False,
    )
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.telemetry.cost_usd == 0.0
    assert outcome.telemetry.cost_source == "local_zero"


# ----------------------------------------------------------- process loss


def test_process_exit_before_settle_is_process_lost():
    """CASE — the child dies mid-run. That is a named state, not 'done'."""
    worker, _ = _worker([{"type": "agent_start"}, assistant("half"), "EOF"])
    outcome = worker.run("go")

    assert outcome.lifecycle == str(Lifecycle.PROCESS_LOST)
    assert outcome.stop_reason == str(StopReason.PROCESS_EXITED)
    assert outcome.final_text is None


# ------------------------------------------------------ real child process


def _run_fake_pi(tmp_path: Path, script: dict, limits: RoleLimits | None = None, paid=False):
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    transport = SubprocessTransport(
        argv=[sys.executable, str(FAKE_PI), "--script", str(script_path)], cwd=tmp_path
    )
    worker = PiRpcWorker(
        transport,
        worker_id="real", role="writer", provider="local_llama", model="qwen3.8-27b",
        limits=limits or RoleLimits(wall_seconds=60, inactivity_seconds=20),
        paid=paid,
    )
    return worker.run("do the bounded work"), transport


def test_a_real_child_process_is_owned_and_closed(tmp_path):
    """Python spawns it, reads it, settles it and closes it. No shell involved."""
    outcome, transport = _run_fake_pi(
        tmp_path,
        {
            "on_prompt": [
                {"type": "agent_start"},
                {"type": "message_start", "message": {"role": "assistant"}},
                {
                    "type": "message_end",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                },
                {"type": "agent_end", "willRetry": False},
                {"type": "agent_settled"},
            ],
            "responses": {"get_last_assistant_text": {"data": {"text": "REAL RESULT"}}},
        },
    )

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.final_text == "REAL RESULT"
    assert outcome.pid and outcome.pid > 0
    assert transport.alive() is False  # closed, not leaked


def test_a_real_child_that_hangs_is_aborted_by_the_watchdog(tmp_path):
    outcome, transport = _run_fake_pi(
        tmp_path,
        {
            "on_prompt": [{"type": "agent_start"}],
            "hang": True,
            "responses": {"get_state": {"data": {"isStreaming": False}}},
        },
        limits=RoleLimits(wall_seconds=30, inactivity_seconds=2),
    )

    assert outcome.lifecycle == str(Lifecycle.TIMED_OUT)
    assert outcome.stop_reason == str(StopReason.INACTIVITY)
    assert transport.alive() is False


def test_a_real_child_that_exits_early_is_process_lost(tmp_path):
    outcome, _ = _run_fake_pi(
        tmp_path,
        {
            "on_prompt": [{"type": "agent_start"}],
            "exit_after": "agent_start",
            "exit_immediately_on_terminal": True,
        },
    )

    assert outcome.lifecycle == str(Lifecycle.PROCESS_LOST)


# ------------------------------------------------------------------- argv


@pytest.mark.parametrize("role_tools", ["none", "edit"])
def test_argv_privilege_is_enforced_by_flags_not_prose(role_tools):
    argv = build_rpc_argv("pi", "openrouter", "deepseek/deepseek-v4-pro-0813", tools=role_tools)

    assert argv[:3] == ["pi", "--mode", "rpc"]
    if role_tools == "none":
        assert "--no-tools" in argv
        assert "--tools" not in argv
    else:
        assert "--no-tools" not in argv
        assert "bash" not in argv[argv.index("--tools") + 1]


def test_argv_names_every_extension_because_discovery_is_disabled():
    argv = build_rpc_argv(
        "pi", "local-llama", "qwen3.8-27b", extensions=["/root/.pi/local-llama-ext.js"]
    )

    assert "--no-extensions" in argv
    assert argv[argv.index("-e") + 1] == "/root/.pi/local-llama-ext.js"
