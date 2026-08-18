"""Read-only preflight.

Reports what the control plane can see. It never prints a secret value, never
dumps the environment, and never makes a paid call: credential state is
reported strictly as YES/NO, and cloud model availability is derived from Pi's
local non-secret catalog.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import gitio
from .adapters import build_adapter
from .config import Config
from .model_router import ModelRouter, Presence, validate_no_claude_runtime
from .run_store import RunStore


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"check": self.name, "ok": self.ok, "detail": self.detail}


def _tool(executable: str, version_args: list[str]) -> Check:
    path = shutil.which(executable)
    if not path:
        return Check(executable, False, "not on PATH")
    proc = subprocess.run(
        [path, *version_args], text=True, capture_output=True, check=False, timeout=60
    )
    output = (proc.stdout or proc.stderr).strip().splitlines()
    return Check(executable, proc.returncode == 0, f"{path} — {output[0] if output else '?'}")


def run(config: Config) -> list[Check]:
    """Every preflight check, in report order."""
    checks: list[Check] = []

    checks.append(_tool("python3", ["--version"]))
    checks.append(_tool("uv", ["--version"]))
    checks.append(_tool("git", ["--version"]))

    adapters = (config.models.get("adapters") or {})
    for name, spec in adapters.items():
        if not spec.get("enabled", False):
            checks.append(Check(f"adapter:{name}", True, "disabled in models config"))
            continue
        ok, detail = build_adapter(name, spec).available()
        checks.append(Check(f"adapter:{name}", ok, detail))

    for name, repo in sorted(config.repos.items()):
        if not repo.path.is_dir():
            checks.append(Check(f"repo:{name}", False, f"{repo.path}: not a directory"))
            continue
        origin = gitio.origin_url(repo.path)
        checks.append(
            Check(
                f"repo:{name}:origin",
                origin == repo.origin,
                f"{origin or '(none)'}" + ("" if origin == repo.origin else f" != {repo.origin}"),
            )
        )
        clean = gitio.is_clean(repo.path)
        checks.append(
            Check(
                f"repo:{name}:worktree",
                clean,
                "clean" if clean else "DIRTY — untracked or modified files present",
            )
        )
        try:
            base_sha = gitio.rev_parse(repo.path, repo.default_base)
            branch = gitio.current_branch(repo.path)
            head = gitio.rev_parse(repo.path, "HEAD")
            checks.append(
                Check(
                    f"repo:{name}:baseline",
                    True,
                    f"{repo.default_base}={base_sha[:12]}  HEAD={head[:12]} on {branch}",
                )
            )
        except Exception as exc:  # reported as a failing check, never swallowed
            checks.append(Check(f"repo:{name}:baseline", False, str(exc)))

    router = ModelRouter(config.models)
    for provider_name, provider in sorted((config.models.get("providers") or {}).items()):
        local = provider.get("kind") == "local"
        if provider.get("api_key_env") and not local:
            present = router.key_available(provider_name)
            checks.append(
                Check(
                    f"provider:{provider_name}:credentials",
                    bool(present),
                    f"{provider['api_key_env']} available: {'YES' if present else 'NO'} "
                    "(value never read or printed)",
                )
            )
        if local:
            # A local provider is registered in Pi BY AN EXTENSION, which
            # resolves its own credential. This orchestrator never reads, stores
            # or prints that value, so there is nothing here to check beyond the
            # extension being present — availability is the per-model HTTP probe.
            extension = provider.get("pi_extension")
            if not extension:
                checks.append(
                    Check(
                        f"provider:{provider_name}:extension",
                        False,
                        "no pi_extension configured; a bounded worker runs with "
                        "--no-extensions, so this provider would not resolve",
                    )
                )
            else:
                present = Path(str(extension)).is_file()
                checks.append(
                    Check(
                        f"provider:{provider_name}:extension",
                        present,
                        f"{extension}" + ("" if present else ": MISSING"),
                    )
                )
            checks.append(
                Check(
                    f"provider:{provider_name}:credentials",
                    True,
                    "resolved by the Pi extension; never read, stored or printed here",
                )
            )
        if provider.get("catalog"):
            catalog_path = Path(str(provider["catalog"]))
            checks.append(
                Check(
                    f"provider:{provider_name}:catalog",
                    catalog_path.is_file(),
                    f"{catalog_path} ({len(router.catalog())} models)"
                    if catalog_path.is_file()
                    else f"{catalog_path}: missing",
                )
            )

    for model_name in sorted(router.models):
        availability = router.availability(model_name)
        presence = availability.presence
        label = {
            str(Presence.AVAILABLE_NOW): "AVAILABLE",
            str(Presence.REGISTERED): "REGISTERED / SERVER OFFLINE",
            str(Presence.CONFIGURED): "CONFIGURED / NOT REACHABLE",
            str(Presence.UNCONFIGURED): "NOT CONFIGURED",
        }.get(presence, presence)
        checks.append(
            Check(f"model:{model_name}", availability.available, f"{label} — {availability.detail}")
        )

    checks.append(_pi_rpc_check(config))
    checks += _routing_checks(config, router)
    checks.append(_no_claude_check(config))
    checks.append(_run_store_check(config))
    return checks


def _pi_rpc_check(config: Config) -> Check:
    """Does ``pi --mode rpc`` actually start and speak the protocol?

    A real spawn, a real ``get_state`` command, a real response — then the child
    is closed. No model is contacted and nothing is spent: ``get_state`` is
    answered by the agent process itself.
    """
    spec = (config.models.get("adapters") or {}).get("pi") or {}
    executable = shutil.which(str(spec.get("executable", "pi")))
    if not executable:
        return Check("pi:rpc", False, "pi is not on PATH")
    try:
        proc = subprocess.Popen(
            [executable, "--mode", "rpc", "--no-session", "--no-extensions", "-nt"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return Check("pi:rpc", False, f"could not spawn: {exc}")
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"id": "doctor", "type": "get_state"}) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "response" and event.get("command") == "get_state":
                return Check("pi:rpc", True, "RPC mode started and answered get_state")
        return Check("pi:rpc", False, "RPC mode started but did not answer get_state in time")
    except OSError as exc:
        return Check("pi:rpc", False, f"RPC handshake failed: {exc}")
    finally:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.stdin.close() if proc.stdin else None
            proc.terminate()
            proc.wait(timeout=5)


def _routing_checks(config: Config, router: ModelRouter) -> list[Check]:
    """Which model each role would actually get, and whether it is reachable."""
    checks: list[Check] = []
    for role in sorted(router.roles):
        primary = (router.roles.get(role) or {}).get("primary", "?")
        availability = router.availability(str(primary))
        limits = router.limits_for(role)
        checks.append(
            Check(
                f"route:{role}",
                availability.available,
                f"{primary} ({availability.presence}) — "
                f"max_calls={limits.max_model_calls} wall={limits.wall_seconds:.0f}s "
                f"hard=${limits.hard_usd:.2f}",
            )
        )
    return checks


def _no_claude_check(config: Config) -> Check:
    """The no-Claude runtime guarantee, asserted rather than asserted-about."""
    refusals = validate_no_claude_runtime(config.models)
    if refusals:
        return Check(
            "runtime:no-claude",
            False,
            f"{len(refusals)} ACTIVE Claude/Anthropic route(s): "
            + "; ".join(refusal.message for refusal in refusals),
        )
    return Check("runtime:no-claude", True, "NONE — no active Claude/Anthropic runtime route")


def _run_store_check(config: Config) -> Check:
    ok, detail = RunStore(config.state_dir / "runs").writable()
    return Check("run-store", ok, detail if ok else f"NOT WRITABLE: {detail}")


def render(checks: list[Check]) -> str:
    lines = ["DOCTOR — read-only preflight", ""]
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        lines.append(f"  [{mark}] {check.name:<38} {check.detail}")
    failures = [check for check in checks if not check.ok]
    lines.append("")
    lines.append(f"{len(checks) - len(failures)}/{len(checks)} checks passed.")
    if failures:
        lines.append("Failing checks are informational for a read-only run; they gate execution.")
    return "\n".join(lines)
