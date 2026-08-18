"""A fake ``pi --mode rpc`` process for tests.

A REAL child process speaking the REAL JSONL protocol, so the supervisor's
process ownership, framing, watchdogs and shutdown are exercised for real —
without a model, a network or a cent.

Driven by a JSON script:

    {
      "on_prompt": [ {event}, {"__sleep__": 0.2}, {event}, ... ],
      "responses": {"get_last_assistant_text": {"data": {"text": "..."}}},
      "exit_after": "agent_settled" | "never",
      "hang": false
    }
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

DEFAULT_RESPONSES = {
    "get_state": {"data": {"isStreaming": False, "isCompacting": False, "messageCount": 1}},
    "get_last_assistant_text": {"data": {"text": "fake assistant text"}},
    "get_session_stats": {
        "data": {
            "toolCalls": 0,
            "tokens": {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0},
            "cost": 0,
        }
    },
    "abort": {},
}


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    script = json.loads(Path(sys.argv[sys.argv.index("--script") + 1]).read_text())
    responses = {**DEFAULT_RESPONSES, **(script.get("responses") or {})}
    exit_after = script.get("exit_after", "agent_settled")

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            command = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = command.get("type", "")

        if name == "prompt":
            emit({"id": command.get("id"), "type": "response", "command": "prompt", "success": True})
            for step in script.get("on_prompt") or []:
                if "__sleep__" in step:
                    time.sleep(float(step["__sleep__"]))
                    continue
                emit(step)
                if step.get("type") == exit_after and script.get("exit_immediately_on_terminal"):
                    return 0
            if script.get("hang"):
                # Deliberately produce nothing more: exercises the inactivity
                # watchdog, which must consult get_state rather than assume.
                while True:
                    time.sleep(0.05)
            continue

        payload = {"type": "response", "command": name, "success": True}
        if command.get("id"):
            payload["id"] = command["id"]
        payload.update(responses.get(name, {}))
        emit(payload)
        if name == "abort" and script.get("exit_on_abort", True):
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
