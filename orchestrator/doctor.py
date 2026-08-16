"""Read-only preflight.

Reports what the control plane can see. It never prints a secret value, never
dumps the environment, and never makes a paid call: credential state is
reported strictly as YES/NO, and cloud model availability is derived from Pi's
local non-secret catalog.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import gitio
from .adapters import build_adapter
from .config import Config
from .model_router import ModelRouter


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
        if provider.get("api_key_env"):
            present = router.key_available(provider_name)
            checks.append(
                Check(
                    f"provider:{provider_name}:credentials",
                    bool(present),
                    f"{provider['api_key_env']} available: {'YES' if present else 'NO'} "
                    "(value never read or printed)",
                )
            )
        if provider.get("kind") == "local":
            model_id = provider.get("model_id")
            candidates = provider.get("discovered_candidates") or []
            if model_id:
                checks.append(
                    Check(f"provider:{provider_name}:model", True, f"model_id={model_id}")
                )
            else:
                labels = "; ".join(str(c.get("label")) for c in candidates)
                checks.append(
                    Check(
                        f"provider:{provider_name}:model",
                        False,
                        f"model_id UNDISCOVERED — {len(candidates)} candidate(s) found, none "
                        f"chosen automatically: {labels or '(none)'}",
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
        checks.append(
            Check(f"model:{model_name}", availability.available, availability.detail)
        )

    return checks


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
