"""Reasoning level is a role property that reaches argv — or is dropped.

Two things must hold. A declared level from Pi's normalized scale reaches the
adapter's argv, so configuration actually changes what runs. An undeclared or
unrecognised level produces no flag at all, because fabricating a provider
parameter is worse than running at the provider default.

The shipped routing table is also asserted here: it is the approved policy, and
a silent edit to it should fail a test rather than change what every future task
is executed by.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.adapters import PiAdapter
from orchestrator.model_router import ModelRouter

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _router(reasoning, environ=None):
    """A router whose single role optionally declares ``reasoning``."""
    role: dict = {"primary": "m", "fallbacks": [], "allow_weaker_fallback": False}
    if reasoning is not None:
        role["reasoning"] = reasoning
    return ModelRouter(
        {
            "providers": {"cloud": {"kind": "cloud", "paid": True, "pi_provider": "openrouter",
                                    "api_key_env": "TEST_KEY"}},
            "models": {"m": {"provider": "cloud", "id": "vendor/m", "family": "vendor",
                             "cost_usd_per_mtok": {"input": 1.0, "output": 1.0}}},
            "roles": {"r": role},
            "escalation": {"low": {"plan": "r"}},
            "privileges": {"plan": {"tools": "none", "bash": False}},
        },
        environ=environ if environ is not None else {"TEST_KEY": "present"},
    )


@pytest.mark.parametrize("level", ["off", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_every_supported_level_reaches_argv(level, tmp_path):
    choice, refusals, _ = _router(level).resolve("plan", "low")
    assert refusals == []
    assert choice is not None and choice.reasoning == level

    argv = PiAdapter({"executable": "pi"}).build_argv(choice, tmp_path / "p.md", "plan")
    assert "--thinking" in argv
    assert argv[argv.index("--thinking") + 1] == level


def test_absent_level_sends_no_flag(tmp_path):
    choice, _, _ = _router(None).resolve("plan", "low")
    assert choice is not None and choice.reasoning is None
    assert "--thinking" not in PiAdapter().build_argv(choice, tmp_path / "p.md", "plan")


def test_unrecognised_level_is_dropped_not_forwarded(tmp_path):
    """An invented level must never reach the provider."""
    choice, _, _ = _router("ludicrous").resolve("plan", "low")
    assert choice is not None and choice.reasoning is None
    assert "--thinking" not in PiAdapter().build_argv(choice, tmp_path / "p.md", "plan")


def test_reasoning_is_reported_in_the_choice_record():
    choice, _, _ = _router("high").resolve("plan", "low")
    assert choice is not None
    assert choice.as_dict()["reasoning"] == "high"


# --------------------------------------------------------------- shipped table


def _shipped() -> dict:
    return yaml.safe_load((CONFIG_DIR / "models.example.yaml").read_text(encoding="utf-8"))


def test_shipped_routing_matches_the_approved_policy():
    roles = _shipped()["roles"]
    approved = {
        "scout": ("deepseek-v4-flash", "medium"),
        "writer": ("glm-5.2", "high"),
        "reviewer": ("deepseek-v4-pro", "high"),
        "cheap_reviewer": ("mimo-v2.5", "medium"),
        "resolver": ("glm-5.2", "xhigh"),
        "resolver_crosscheck": ("deepseek-v4-pro", "xhigh"),
    }
    for role, (model, reasoning) in approved.items():
        assert roles[role]["primary"] == model, role
        assert roles[role].get("reasoning") == reasoning, role


def test_shipped_reasoning_levels_are_all_on_pis_scale():
    for role, spec in _shipped()["roles"].items():
        level = spec.get("reasoning")
        assert level is None or level in ModelRouter.REASONING_LEVELS, role


def test_writer_and_reviewer_are_different_families():
    """An independent review must not share the writer's blind spots."""
    config = _shipped()
    models, roles = config["models"], config["roles"]
    writer = models[roles["writer"]["primary"]]["family"]
    reviewer = models[roles["reviewer"]["primary"]]["family"]
    assert writer != reviewer


def test_claude_is_a_fallback_and_never_a_primary():
    config = _shipped()
    primaries = {spec["primary"] for spec in config["roles"].values()}
    assert "claude-sonnet-5" not in primaries
    assert any("claude-sonnet-5" in (spec.get("fallbacks") or [])
               for spec in config["roles"].values())


def test_resolver_roles_refuse_rather_than_substitute():
    roles = _shipped()["roles"]
    for role in ("resolver", "resolver_crosscheck"):
        assert roles[role]["allow_weaker_fallback"] is False, role
        assert roles[role]["fallbacks"] == [], role


def test_every_escalation_target_is_a_defined_role():
    config = _shipped()
    for complexity, stages in config["escalation"].items():
        for stage, role in stages.items():
            assert role in config["roles"], f"{complexity}.{stage} -> {role}"


def test_every_role_primary_is_a_defined_model():
    config = _shipped()
    for role, spec in config["roles"].items():
        assert spec["primary"] in config["models"], role
        for fallback in spec.get("fallbacks") or []:
            assert fallback in config["models"], f"{role} -> {fallback}"
