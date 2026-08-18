"""Optional LIVE smoke test against the local GPU model.

Skipped unless BOTH are true:

* ``RUN_LIVE_PI_SMOKE=1`` is set — this must never run by accident;
* the configured local model's server actually answers a health probe.

It spends nothing: the model is local, so ``cost_source`` is ``local_zero`` and
external API spend is $0. It never falls back to OpenRouter — if the GPU server
is stopped the test SKIPS, because "the local tier is offline" is a fact about
infrastructure, not a licence to spend money proving a point.
"""

from __future__ import annotations

import os

import pytest
import yaml

from orchestrator.config import CONFIG_DIR
from orchestrator.model_router import ModelRouter
from orchestrator.pi_rpc import (
    Lifecycle,
    PiRpcWorker,
    RoleLimits,
    StopReason,
    SubprocessTransport,
    build_rpc_argv,
)

LOCAL_MODEL = "qwen3.8-27b"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PI_SMOKE") != "1",
    reason="live smoke test; set RUN_LIVE_PI_SMOKE=1 to enable",
)


@pytest.fixture(scope="module")
def local_route():
    config = yaml.safe_load((CONFIG_DIR / "models.example.yaml").read_text(encoding="utf-8"))
    router = ModelRouter(config, dict(os.environ))
    availability = router.availability(LOCAL_MODEL)
    if not availability.available:
        pytest.skip(f"SKIPPED / LOCAL MODEL OFFLINE — {availability.detail}")
    provider = config["providers"][config["models"][LOCAL_MODEL]["provider"]]
    return config, provider


def test_a_live_local_worker_settles_and_costs_nothing(local_route, tmp_path):
    _config, provider = local_route
    argv = build_rpc_argv(
        "pi",
        provider["pi_provider"],
        LOCAL_MODEL,
        tools="none",
        extensions=[str(provider["pi_extension"])],
    )
    worker = PiRpcWorker(
        SubprocessTransport(argv=argv, cwd=tmp_path),
        worker_id="live-smoke",
        role="scout",
        provider="local_llama",
        model=LOCAL_MODEL,
        limits=RoleLimits(max_model_calls=2, max_tool_calls=0, wall_seconds=180, inactivity_seconds=90),
        paid=False,
    )

    outcome = worker.run("Reply with exactly this and nothing else: LIVE_SMOKE_OK")

    assert outcome.lifecycle == str(Lifecycle.COMPLETE)
    assert outcome.stop_reason == str(StopReason.SETTLED)
    assert outcome.final_text is not None
    # Zero EXTERNAL API spend. Not a claim that the GPU was free.
    assert outcome.telemetry.cost_usd == 0.0
    assert outcome.telemetry.cost_source == "local_zero"
    # The process was owned and closed, not leaked.
    assert outcome.pid and outcome.pid > 0
    assert outcome.exit_code is not None
