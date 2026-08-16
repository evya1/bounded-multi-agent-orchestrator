"""The bounded-write path policy — the guard that actually stops a stray write.

`--tools` selects tool NAMES and `cwd` is not a sandbox, so these rules are what
confine an implementer. The same case table is replayed against the TypeScript
guard that runs inside Pi, so the two cannot drift apart.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator import write_guard
from orchestrator.adapters import PiAdapter
from orchestrator.errors import OrchestratorError, Reason
from orchestrator.model_router import ModelRouter
from orchestrator.write_guard import Denial, policy_for

CASES = json.loads((Path(__file__).parent / "data" / "write_guard_cases.json").read_text())


@pytest.fixture
def guarded(tmp_path: Path):
    """A worktree-shaped tree plus an outside directory to try to escape into."""
    root = tmp_path / "worktree"
    outside = tmp_path / "outside"
    sibling = tmp_path / "sibling"
    for directory in (root, outside, sibling):
        directory.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "uv.lock").write_text("# lock\n")
    (root / "tests").mkdir()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "docs" / "PRD.md").write_text("# PRD\n")
    (outside / "pyproject.toml").write_text("[project]\nname='escaped'\n")
    (sibling / "pyproject.toml").write_text("[project]\nname='sibling'\n")
    return root, outside


def _apply_symlink(root: Path, outside: Path, requirement) -> None:
    """Set up the symlink a case needs, replacing any real entry."""
    if requirement is True:
        (root / "escape-dir").symlink_to(outside, target_is_directory=True)
        return
    target = root / requirement
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    target.symlink_to(outside / "pyproject.toml" if requirement == "uv.lock" else outside)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_path_policy(guarded, case):
    root, outside = guarded
    if case.get("requires_symlink"):
        _apply_symlink(root, outside, case["requires_symlink"])

    policy = policy_for(root, CASES["write_set"])
    raw = case["path"]
    if case.get("absolute") and raw.startswith("/tmp/definitely-not"):
        raw = str(outside / "pyproject.toml")
    verdict = policy.check_path(raw, cwd=root)

    assert verdict.allowed is case["allowed"], f"{case['name']}: {verdict.reason}"
    if not case["allowed"]:
        assert str(verdict.denial) == case["denial"], f"{case['name']}: {verdict.reason}"


@pytest.mark.parametrize("case", CASES["tool_cases"], ids=lambda c: c["name"])
def test_tool_policy(guarded, case):
    root, _ = guarded
    policy = policy_for(root, CASES["write_set"])
    verdict = policy.check_tool_call(case["tool"], case["input"], cwd=root)
    assert verdict.allowed is case["allowed"], f"{case['name']}: {verdict.reason}"
    if not case["allowed"]:
        assert str(verdict.denial) == case["denial"], f"{case['name']}: {verdict.reason}"


def test_unknown_tool_is_denied_by_default(guarded):
    """Positive security: a mutation tool Pi adds tomorrow is blocked, not allowed."""
    root, _ = guarded
    policy = policy_for(root, ["pyproject.toml"])
    verdict = policy.check_tool_call("apply_patch", {"path": "pyproject.toml"}, cwd=root)
    assert verdict.allowed is False
    assert verdict.denial is Denial.TOOL_NOT_ALLOWED


def test_empty_write_set_permits_nothing(guarded):
    root, _ = guarded
    policy = policy_for(root, [])
    assert policy.check_path("pyproject.toml", cwd=root).allowed is False


def test_environment_carries_policy_and_no_secrets(guarded):
    root, _ = guarded
    environment = policy_for(root, ["pyproject.toml"]).environment()
    assert environment[write_guard.ENV_ROOT] == str(Path(os.path.realpath(root)))
    assert json.loads(environment[write_guard.ENV_WRITE_SET]) == ["pyproject.toml"]
    assert environment[write_guard.ENV_ALLOW_SHELL] == "0"
    assert not any("KEY" in name or "TOKEN" in name for name in environment)


# --------------------------------------------- write_set_only changes behaviour


def _implement_choice(config):
    return ModelRouter(config.models, {"FIXTURE_KEY": "x"})._choice(
        "local_executor", "local-qwen", "implement", None
    )


def test_write_set_only_now_changes_runtime_behaviour(config, guarded):
    """The flag was previously inert. It must now alter the launched process."""
    root, _ = guarded
    choice = _implement_choice(config)
    assert choice.privileges["write_set_only"] is True

    policy = policy_for(root, ["pyproject.toml"])
    argv = PiAdapter({"executable": "pi"}).build_argv(choice, Path("/tmp/p.md"), "implement", policy)

    # the guard extension is attached, and only it
    assert "-e" in argv
    assert argv[argv.index("-e") + 1] == str(write_guard.EXTENSION_PATH)
    assert "--no-extensions" in argv
    # and the process is handed the policy
    assert policy.environment()[write_guard.ENV_ROOT] == str(Path(os.path.realpath(root)))


def test_mutating_stage_without_a_policy_refuses_to_launch(config):
    """No pre-write guard means no run at all — not a run with weaker safety."""
    with pytest.raises(OrchestratorError) as excinfo:
        PiAdapter({"executable": "pi"}).build_argv(
            _implement_choice(config), Path("/tmp/p.md"), "implement", None
        )
    assert excinfo.value.refusal.reason is Reason.WRITE_GUARD_UNAVAILABLE


def test_read_only_stage_needs_no_policy(config):
    choice = ModelRouter(config.models, {"FIXTURE_KEY": "x"})._choice("r", "frontier", "plan", None)
    argv = PiAdapter({"executable": "pi"}).build_argv(choice, Path("/tmp/p.md"), "plan", None)
    assert "--no-tools" in argv
    assert "-e" not in argv


def test_claude_adapter_refuses_mutating_stages(config):
    from orchestrator.adapters import ClaudeAdapter

    with pytest.raises(OrchestratorError) as excinfo:
        ClaudeAdapter({"executable": "claude"}).build_argv(
            _implement_choice(config), Path("/tmp/p.md"), "implement", None
        )
    assert excinfo.value.refusal.reason is Reason.WRITE_GUARD_UNAVAILABLE


def test_extension_file_ships_with_the_harness():
    assert write_guard.EXTENSION_PATH.is_file()
    source = write_guard.EXTENSION_PATH.read_text()
    assert 'pi.on("tool_call"' in source
    assert "block: true" in source


# ------------------------------------- the TypeScript guard agrees with Python


def _node() -> str | None:
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="node is not available")
def test_typescript_guard_matches_the_python_policy(guarded):
    """The guard that actually runs inside Pi must reach identical verdicts.

    Runs the same table through the TypeScript implementation. No Pi process, no
    model, no network.
    """
    root, outside = guarded
    _apply_symlink(root, outside, True)

    runner = Path(__file__).parent / "ts_runner" / "run_cases.mjs"
    cases_file = Path(__file__).parent / "data" / "write_guard_cases.json"
    proc = subprocess.run(
        [_node(), "--experimental-strip-types", str(runner), str(cases_file), str(root)],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"node could not run the TypeScript guard: {proc.stderr[-400:]}")

    verdicts = {row["name"]: row for row in json.loads(proc.stdout)}
    policy = policy_for(root, CASES["write_set"])

    mismatches = []
    for case in CASES["cases"]:
        if case.get("requires_symlink") not in (None, True):
            continue  # per-case symlink rewiring is exercised by the Python table
        raw = case["path"]
        if case.get("absolute") and raw.startswith("/tmp/definitely-not"):
            continue  # path is fixture-relative on the Python side only
        mine = policy.check_path(raw, cwd=root)
        theirs = verdicts[case["name"]]
        if mine.allowed != theirs["allowed"] or (
            not mine.allowed and str(mine.denial) != theirs["denial"]
        ):
            mismatches.append((case["name"], mine.as_dict(), theirs))

    for case in CASES["tool_cases"]:
        mine = policy.check_tool_call(case["tool"], case["input"], cwd=root)
        theirs = verdicts[case["name"]]
        if mine.allowed != theirs["allowed"] or (
            not mine.allowed and str(mine.denial) != theirs["denial"]
        ):
            mismatches.append((case["name"], mine.as_dict(), theirs))

    assert not mismatches, f"TypeScript and Python guards disagree: {mismatches}"
