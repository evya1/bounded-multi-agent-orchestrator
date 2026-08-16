"""Model execution adapters.

The orchestrator does not know how to talk to a model; an adapter does. Routing
policy stays in YAML, argv construction stays here, and tests inject
``FakeRunner`` so no test ever spends a cent.

Privilege enforcement is argv-level and hook-level, never prose-level:

* planner and reviewer run with ``--no-tools`` — the process is handed no tool
  at all, so "the prompt told it not to write" never has to be trusted;
* the implementer gets a tool allowlist without ``bash``, PLUS the bounded-write
  Pi extension, which blocks each ``write``/``edit`` call whose resolved path
  falls outside the task worktree or outside the declared write_set;
* whatever the model did is then audited deterministically by ``verifier``.

``cwd`` is **not** a sandbox and ``--tools`` restricts tool *names* only; the
extension's blocking ``tool_call`` hook is what actually confines writes. The
post-run Git audit is defence in depth, not the enforcement mechanism.

This is defence against ACCIDENTAL scope escape. It is not a hostile-code
sandbox: Pi runs with the invoking user's permissions.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import OrchestratorError, Reason
from .write_guard import EXTENSION_PATH, PathPolicy

STOP_TOKEN = "STOP_NEEDS_ORCHESTRATOR"


@dataclass
class RunResult:
    """What one model invocation produced. No prompt text, no credentials."""

    exit_code: int
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_cost_usd: float | None = None
    duration_s: float = 0.0
    argv: list[str] = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        """Did the worker raise the stop-and-escalate contract?"""
        return STOP_TOKEN in self.text

    def as_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reported_cost_usd": self.reported_cost_usd,
            "duration_s": round(self.duration_s, 2),
            "stopped": self.stopped,
        }


class Adapter:
    """Interface every model runner implements."""

    name = "adapter"

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def build_argv(
        self, choice, prompt_file: Path, stage: str, policy: PathPolicy | None = None
    ) -> list[str]:
        raise NotImplementedError

    def run(
        self,
        choice,
        prompt_file: Path,
        stage: str,
        cwd: Path,
        timeout: int,
        policy: PathPolicy | None = None,
    ) -> RunResult:
        raise NotImplementedError


class PiAdapter(Adapter):
    """Drives the installed Pi CLI (verified against Pi 0.84.2's own --help)."""

    name = "pi"

    def __init__(self, spec: dict | None = None) -> None:
        spec = spec or {}
        self.executable = spec.get("executable", "pi")

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.executable)
        if not path:
            return False, f"{self.executable}: not on PATH"
        proc = subprocess.run(
            [path, "--version"], text=True, capture_output=True, check=False, timeout=60
        )
        return proc.returncode == 0, f"{path}: {proc.stdout.strip() or proc.stderr.strip()}"

    def build_argv(
        self, choice, prompt_file: Path, stage: str, policy: PathPolicy | None = None
    ) -> list[str]:
        argv = [
            self.executable,
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--provider",
            choice.provider_pi_name,
            "--model",
            choice.model_id or "",
        ]
        privileges = choice.privileges or {}
        tools = privileges.get("tools", "none")
        if tools == "none":
            argv.append("--no-tools")
        else:
            allowed = ["read", "ls", "find", "grep", "edit", "write"] if tools == "edit" else list(tools)
            if privileges.get("bash"):
                allowed.append("bash")
            argv += ["--tools", ",".join(allowed)]

        if privileges.get("write_set_only"):
            # `--tools` selects tool NAMES only, and cwd is not a sandbox. The
            # actual path boundary is this extension's blocking `tool_call`
            # hook. Refuse to launch a mutating stage without it.
            if policy is None:
                raise OrchestratorError(
                    Reason.WRITE_GUARD_UNAVAILABLE,
                    f"stage {stage!r} declares write_set_only but no path policy was supplied; "
                    "refusing to run a mutating model without a pre-write guard",
                )
            if not EXTENSION_PATH.is_file():
                raise OrchestratorError(
                    Reason.WRITE_GUARD_UNAVAILABLE,
                    f"bounded-write guard extension is missing at {EXTENSION_PATH}",
                )
            # `--no-extensions` stops any other discovered extension from
            # loading; the explicit `-e` still applies.
            argv += ["--no-extensions", "-e", str(EXTENSION_PATH)]
        argv += [f"@{prompt_file}"]
        return argv

    def run(
        self,
        choice,
        prompt_file: Path,
        stage: str,
        cwd: Path,
        timeout: int = 3600,
        policy: PathPolicy | None = None,
    ) -> RunResult:
        argv = self.build_argv(choice, prompt_file, stage, policy)
        environment = dict(os.environ)
        if (choice.privileges or {}).get("write_set_only") and policy is not None:
            environment.update(policy.environment(allow_shell=bool(choice.privileges.get("bash"))))
        started = time.monotonic()
        proc = subprocess.run(
            argv, cwd=cwd, text=True, capture_output=True, check=False, timeout=timeout,
            env=environment,
        )
        result = parse_pi_json(proc.stdout)
        result.exit_code = proc.returncode
        result.duration_s = time.monotonic() - started
        result.argv = argv
        if not result.text:
            result.text = proc.stderr[-4000:]
        return result


def parse_pi_json(stream: str) -> RunResult:
    """Parse Pi's ``--mode json`` event stream.

    Assistant text is assembled from ``message_end`` events; usage comes from the
    latest cumulative ``usage`` record, which carries provider token counts and
    Pi's own cost arithmetic.
    """
    text_parts: list[str] = []
    usage: dict = {}
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if event.get("type") == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts += [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
    cost = usage.get("cost") or {}
    return RunResult(
        exit_code=0,
        text="\n".join(part for part in text_parts if part).strip(),
        input_tokens=int(usage.get("input", 0) or 0),
        output_tokens=int(usage.get("output", 0) or 0),
        cache_read_tokens=int(usage.get("cacheRead", 0) or 0),
        cache_write_tokens=int(usage.get("cacheWrite", 0) or 0),
        reported_cost_usd=float(cost["total"]) if isinstance(cost.get("total"), int | float) else None,
    )


class ClaudeAdapter(Adapter):
    """Optional separate adapter for the ``claude`` CLI."""

    name = "claude"

    def __init__(self, spec: dict | None = None) -> None:
        self.executable = (spec or {}).get("executable", "claude")

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.executable)
        if not path:
            return False, f"{self.executable}: not on PATH"
        proc = subprocess.run(
            [path, "--version"], text=True, capture_output=True, check=False, timeout=60
        )
        return proc.returncode == 0, f"{path}: {proc.stdout.strip()}"

    def build_argv(
        self, choice, prompt_file: Path, stage: str, policy: PathPolicy | None = None
    ) -> list[str]:
        if (choice.privileges or {}).get("write_set_only"):
            raise OrchestratorError(
                Reason.WRITE_GUARD_UNAVAILABLE,
                "the claude adapter has no bounded-write guard; it may not run a mutating stage",
            )
        argv = [self.executable, "-p", "--output-format", "json"]
        if (choice.privileges or {}).get("tools", "none") == "none":
            argv += ["--allowedTools", ""]
        return argv

    def run(
        self,
        choice,
        prompt_file: Path,
        stage: str,
        cwd: Path,
        timeout: int = 3600,
        policy: PathPolicy | None = None,
    ) -> RunResult:
        argv = self.build_argv(choice, prompt_file, stage, policy)
        started = time.monotonic()
        proc = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            input=prompt_file.read_text(encoding="utf-8"),
        )
        return RunResult(
            exit_code=proc.returncode,
            text=proc.stdout.strip() or proc.stderr[-4000:],
            duration_s=time.monotonic() - started,
            argv=argv,
        )


class FakeRunner(Adapter):
    """Deterministic adapter for tests. Never touches a network or a wallet."""

    name = "fake"

    def __init__(self, result: RunResult | None = None) -> None:
        self.result = result or RunResult(exit_code=0, text="fake plan")
        self.calls: list[dict] = []

    def available(self) -> tuple[bool, str]:
        return True, "fake adapter"

    def build_argv(
        self, choice, prompt_file: Path, stage: str, policy: PathPolicy | None = None
    ) -> list[str]:
        return ["fake", stage, choice.name]

    def run(
        self,
        choice,
        prompt_file: Path,
        stage: str,
        cwd: Path,
        timeout: int = 0,
        policy: PathPolicy | None = None,
    ) -> RunResult:
        self.calls.append(
            {
                "stage": stage,
                "model": choice.name,
                "cwd": str(cwd),
                "policy_root": str(policy.root) if policy else None,
                "policy_write_set": list(policy.write_set) if policy else None,
            }
        )
        return self.result


def build_adapter(name: str, spec: dict | None = None) -> Adapter:
    adapters = {"pi": PiAdapter, "claude": ClaudeAdapter, "fake": FakeRunner}
    if name not in adapters:
        raise KeyError(f"unknown adapter {name!r}; known: {sorted(adapters)}")
    return adapters[name]() if name == "fake" else adapters[name](spec)
