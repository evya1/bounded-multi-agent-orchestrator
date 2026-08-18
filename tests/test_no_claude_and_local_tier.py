"""Routing hardening: no Claude at runtime, and registration != availability.

CASE H — a registered local model whose server is offline must be UNAVAILABLE,
         with no silent fall back to a paid remote model.
CASE J — an active configuration that can reach Claude must FAIL validation.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from orchestrator.config import CONFIG_DIR, load_models_config
from orchestrator.errors import Reason
from orchestrator.model_router import (
    ModelRouter,
    Presence,
    validate_no_claude_runtime,
)

SHIPPED = yaml.safe_load((CONFIG_DIR / "models.example.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ CASE J


def test_the_shipped_configuration_has_no_claude_runtime_route():
    assert validate_no_claude_runtime(SHIPPED) == []


def test_the_loaded_live_configuration_has_no_claude_runtime_route():
    """Whatever models.yaml a machine actually uses, this must still hold."""
    assert validate_no_claude_runtime(load_models_config()) == []


def test_no_shipped_model_is_an_anthropic_model():
    for name, spec in SHIPPED["models"].items():
        blob = f"{name} {spec.get('id')} {spec.get('family')}".lower()
        for marker in ("anthropic", "claude", "opus", "sonnet", "haiku"):
            assert marker not in blob, f"{name} looks like an Anthropic model"


def test_no_adapter_executes_the_claude_cli():
    for name, spec in (SHIPPED.get("adapters") or {}).items():
        assert "claude" not in f"{name} {spec.get('executable', '')}".lower()


@pytest.mark.parametrize(
    "mutation",
    [
        {"roles": {"reviewer": {"primary": "sneaky-claude"}}},
        {"roles": {"reviewer": {"fallbacks": ["sneaky-claude"]}}},
        {"roles": {"writer": {"on_unavailable": "sneaky-claude"}}},
    ],
    ids=["primary", "fallback", "unknown-role-default"],
)
def test_a_claude_route_anywhere_fails_validation(mutation):
    """CASE J — every place a route can hide is checked, not just `primary`."""
    config = copy.deepcopy(SHIPPED)
    config["models"]["sneaky-claude"] = {
        "provider": "openrouter",
        "id": "anthropic/claude-sonnet-5",
        "family": "anthropic",
    }
    for role, spec in mutation["roles"].items():
        config["roles"][role].update(spec)

    refusals = validate_no_claude_runtime(config)

    assert refusals, "a Claude route was not detected"
    assert all(refusal.reason == Reason.CONFIG_INVALID for refusal in refusals)


def test_an_enabled_claude_adapter_fails_validation():
    config = copy.deepcopy(SHIPPED)
    config["adapters"]["claude"] = {"executable": "claude", "enabled": True}

    assert validate_no_claude_runtime(config)


def test_a_disabled_claude_adapter_is_not_a_runtime_route():
    """The requirement is 'no ACTIVE route', not 'the word may not appear'."""
    config = copy.deepcopy(SHIPPED)
    config["adapters"]["claude"] = {"executable": "claude", "enabled": False}

    assert validate_no_claude_runtime(config) == []


def test_documentation_mentioning_claude_is_not_a_finding():
    config = copy.deepcopy(SHIPPED)
    config["notes"] = "Historically this routed to claude-sonnet-5. It no longer does."

    assert validate_no_claude_runtime(config) == []


# ------------------------------------------------------------------ CASE H


def _router(environ=None, probe=None, monkeypatch=None):
    if probe is not None and monkeypatch is not None:
        monkeypatch.setattr("orchestrator.model_router._probe_http", probe)
    router = ModelRouter(copy.deepcopy(SHIPPED), environ or {"OPENROUTER_API_KEY": "x"})
    router._catalog = {spec["id"]: {} for spec in SHIPPED["models"].values() if spec.get("id")}
    return router


def test_a_registered_local_model_with_an_offline_server_is_unavailable(monkeypatch):
    """CASE H — being in Pi's extension is not being reachable."""
    router = _router(
        probe=lambda url, timeout=2.0: (False, "unreachable: [Errno 111] Connection refused"),
        monkeypatch=monkeypatch,
    )

    availability = router.availability("qwen3-coder-30b")

    assert availability.available is False
    assert availability.presence == str(Presence.REGISTERED)
    assert "SERVER OFFLINE" in availability.detail


def test_an_available_local_model_is_detected(monkeypatch):
    router = _router(probe=lambda url, timeout=2.0: (True, "HTTP 200"), monkeypatch=monkeypatch)

    availability = router.availability("qwen3.8-27b")

    assert availability.available is True
    assert availability.presence == str(Presence.AVAILABLE_NOW)


def test_each_local_model_is_probed_at_its_own_port(monkeypatch):
    """One running server must not make the other three look up."""
    probed: list[str] = []

    def probe(url, timeout=2.0):
        probed.append(url)
        return ("8093" in url), "HTTP 200" if "8093" in url else "refused"

    router = _router(probe=probe, monkeypatch=monkeypatch)
    results = {
        name: router.availability(name).available
        for name in ("qwen3.8-27b", "qwen3.6-35b", "qwen3-coder-30b", "kat-coder-v2.5-dev")
    }

    assert results == {
        "qwen3.8-27b": True,
        "qwen3.6-35b": False,
        "qwen3-coder-30b": False,
        "kat-coder-v2.5-dev": False,
    }
    assert len(set(probed)) == 4, "each model must get its own probe"


def test_an_offline_local_writer_does_not_silently_become_a_paid_remote_model(monkeypatch):
    """CASE H — local unavailability is a refusal, never a promotion."""
    router = _router(probe=lambda url, timeout=2.0: (False, "refused"), monkeypatch=monkeypatch)
    router.escalation["medium"]["implement"] = "writer_local"

    choice, refusals, _ = router.resolve("implement", "medium")

    assert choice is None
    assert refusals and refusals[0].reason == Reason.MODEL_UNAVAILABLE


def test_a_local_model_records_zero_external_api_spend():
    router = _router()

    assert router.is_paid("scout") is False
    assert router.limits_for("scout").hard_usd == 0.0
    assert router.limits_for("scout").soft_usd == 0.0


def test_a_local_role_still_has_non_cost_limits():
    """Zero dollars is not permission to run forever."""
    limits = _router().limits_for("scout")

    assert limits.max_model_calls > 0
    assert limits.wall_seconds > 0
    assert limits.inactivity_seconds > 0


# ------------------------------------------------------------- fail closed


def test_an_unknown_role_fails_closed():
    router = _router()

    choice, refusals, _ = router.resolve("nonexistent_stage", "medium")

    assert choice is None
    assert refusals[0].reason == Reason.CONFIG_INVALID


def test_an_unknown_complexity_fails_closed():
    router = _router()

    choice, refusals, _ = router.resolve("implement", "cosmic")

    assert choice is None
    assert refusals[0].reason == Reason.CONFIG_INVALID


def test_a_missing_model_fails_closed():
    router = _router()
    router.models["gemini-3.7-flash"]["id"] = "google/does-not-exist"

    choice, refusals, _ = router.resolve("implement", "medium")

    assert choice is None
    assert refusals[0].reason == Reason.MODEL_UNAVAILABLE


def test_no_shipped_role_can_substitute_across_families():
    router = _router()
    # Give the reviewer a cross-family fallback WITHOUT the explicit permission.
    router.roles["reviewer"]["fallbacks"] = ["glm-5.2"]
    router.roles["reviewer"]["allow_weaker_fallback"] = True
    router.models["deepseek-v4-pro"]["id"] = "deepseek/does-not-exist"

    choice, _refusals, notes = router.resolve("review", "medium")

    assert choice is None
    assert any("family" in note and "REFUSED" in note for note in notes)


def test_writer_and_reviewer_come_from_different_families():
    models, roles = SHIPPED["models"], SHIPPED["roles"]

    assert (
        models[roles["writer"]["primary"]]["family"]
        != models[roles["reviewer"]["primary"]]["family"]
    )


def test_the_resolver_is_a_third_family():
    models, roles = SHIPPED["models"], SHIPPED["roles"]
    families = {
        models[roles[role]["primary"]]["family"] for role in ("writer", "reviewer", "resolver")
    }

    assert len(families) == 3
