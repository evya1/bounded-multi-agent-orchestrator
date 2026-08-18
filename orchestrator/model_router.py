"""Model routing as configuration.

Roles map to models in YAML. Python decides *which role* a stage needs and
whether the chosen model is actually available; it never contains a provider
name. Three properties matter:

* a frontier reviewer is never silently downgraded — an unavailable primary
  with ``allow_weaker_fallback: false`` is a refusal, not a substitution;
* every substitution that does happen is reported;
* for high-risk work, plan / implement / review should not all come from one
  model family, and a loss of independence is stated out loud.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Reason, Refusal

RISK_TO_COMPLEXITY = {"low": "low", "medium": "medium", "high": "high", "very_high": "very_high"}


@dataclass(frozen=True)
class ModelChoice:
    """One resolved model for one stage, with its cost and privilege profile."""

    role: str
    name: str
    provider: str
    provider_kind: str
    provider_pi_name: str
    model_id: str | None
    family: str
    paid: bool
    price: dict
    substituted_for: str | None = None
    privileges: dict = field(default_factory=dict)
    #: Pi's normalized --thinking level for this ROLE, or None for the
    #: provider default. A role property, not a model property: one model
    #: serves the writer at `high` and the resolver at `xhigh`.
    reasoning: str | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model_id or '<undiscovered>'}"

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "model": self.name,
            "provider": self.provider,
            "provider_kind": self.provider_kind,
            "provider_pi_name": self.provider_pi_name,
            "model_id": self.model_id,
            "family": self.family,
            "paid": self.paid,
            "price_usd_per_mtok": self.price,
            "substituted_for": self.substituted_for,
            "privileges": self.privileges,
            "reasoning": self.reasoning,
        }


@dataclass
class Availability:
    """Whether one configured model can actually be reached right now."""

    name: str
    available: bool
    detail: str

    def as_dict(self) -> dict:
        return {"model": self.name, "available": self.available, "detail": self.detail}


def _load_catalog(path: str | Path) -> dict[str, dict]:
    """Read Pi's non-secret model store. Contains no credentials."""
    target = Path(path)
    if not target.is_file():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    catalog: dict[str, dict] = {}
    for provider in data.values():
        for model in provider.get("models", []):
            catalog[model["id"]] = model
    return catalog


def _probe_http(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        # Local, operator-configured base URL only; never a user-supplied scheme.
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500, f"HTTP {response.status}"
    except urllib.error.URLError as exc:
        return False, f"unreachable: {exc.reason}"
    except OSError as exc:
        return False, f"unreachable: {exc}"


class ModelRouter:
    """Resolves (stage, complexity) -> role -> model, and checks availability."""

    def __init__(self, models_config: dict, environ: dict | None = None) -> None:
        self.config = models_config
        self.environ = environ if environ is not None else os.environ
        self.models: dict[str, dict] = models_config.get("models") or {}
        self.providers: dict[str, dict] = models_config.get("providers") or {}
        self.roles: dict[str, dict] = models_config.get("roles") or {}
        self.escalation: dict[str, dict] = models_config.get("escalation") or {}
        self.privileges: dict[str, dict] = models_config.get("privileges") or {}
        self._catalog: dict[str, dict] | None = None

    # ------------------------------------------------------------- catalogue

    def catalog(self) -> dict[str, dict]:
        if self._catalog is None:
            self._catalog = {}
            for provider in self.providers.values():
                if provider.get("catalog"):
                    self._catalog.update(_load_catalog(provider["catalog"]))
        return self._catalog

    def key_available(self, provider_name: str) -> bool | None:
        """Is a credential present? Returns availability only — never the value."""
        provider = self.providers.get(provider_name) or {}
        env_name = provider.get("api_key_env")
        if not env_name:
            return None
        return bool(self.environ.get(env_name))

    def availability(self, model_name: str) -> Availability:
        """Can this model be reached, without spending anything to find out?"""
        model = self.models.get(model_name)
        if model is None:
            return Availability(model_name, False, "not defined in the routing table")
        provider_name = model.get("provider", "")
        provider = self.providers.get(provider_name) or {}

        if provider.get("kind") == "local":
            model_id = provider.get("model_id")
            if not model_id:
                candidates = provider.get("discovered_candidates") or []
                return Availability(
                    model_name,
                    False,
                    "local provider model_id is UNDISCOVERED; "
                    f"{len(candidates)} candidate local model(s) found but none chosen — "
                    "a human must set providers.%s.model_id" % provider_name,
                )
            base_url = self.environ.get(provider.get("base_url_env", ""), "") or provider.get(
                "default_base_url", ""
            )
            if not base_url:
                return Availability(model_name, False, "no local base URL configured")
            ok, detail = _probe_http(base_url.rstrip("/") + "/v1/models")
            return Availability(model_name, ok, f"{base_url}: {detail}")

        if not self.key_available(provider_name):
            return Availability(
                model_name, False, f"{provider_name}: credentials not configured"
            )
        model_id = model.get("id")
        catalog = self.catalog()
        if catalog and model_id not in catalog:
            return Availability(
                model_name, False, f"{model_id}: absent from the {provider_name} catalog"
            )
        return Availability(model_name, True, f"{model_id}: present in the {provider_name} catalog")

    # ------------------------------------------------------------ resolution

    def role_for(self, stage: str, complexity: str) -> str | None:
        return (self.escalation.get(complexity) or {}).get(stage)

    REASONING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

    def _reasoning_for(self, role: str) -> str | None:
        """The role's declared reasoning level, validated against Pi's scale.

        An unknown level is dropped rather than passed through: fabricating a
        provider parameter is worse than running at the provider default.
        """
        level = (self.roles.get(role) or {}).get("reasoning")
        if level is None:
            return None
        return str(level) if str(level) in self.REASONING_LEVELS else None

    def _choice(self, role: str, model_name: str, stage: str, substituted_for: str | None) -> ModelChoice:
        model = self.models[model_name]
        provider_name = model.get("provider", "")
        provider = self.providers.get(provider_name) or {}
        return ModelChoice(
            role=role,
            name=model_name,
            provider=provider_name,
            provider_kind=str(provider.get("kind", "unknown")),
            provider_pi_name=str(provider.get("pi_provider", provider_name)),
            model_id=model.get("id") or provider.get("model_id"),
            family=str(model.get("family", "unknown")),
            paid=bool(provider.get("paid", True)),
            price=dict(model.get("cost_usd_per_mtok") or {}),
            substituted_for=substituted_for,
            privileges=dict(self.privileges.get(stage) or {}),
            reasoning=self._reasoning_for(role),
        )

    def resolve(
        self, stage: str, complexity: str, override: str | None = None
    ) -> tuple[ModelChoice | None, list[Refusal], list[str]]:
        """Pick the model for a stage. Returns (choice, refusals, notes)."""
        notes: list[str] = []
        if override:
            if override not in self.models:
                return None, [
                    Refusal(
                        Reason.MODEL_UNAVAILABLE,
                        f"manual override {override!r} is not in the routing table",
                    )
                ], notes
            notes.append(f"manual override: {override}")
            choice = self._choice("manual_override", override, stage, None)
            check = self.availability(override)
            if not check.available:
                return None, [
                    Refusal(
                        Reason.MODEL_UNAVAILABLE,
                        f"override {override}: {check.detail}",
                        check.as_dict(),
                    )
                ], notes
            return choice, [], notes

        role = self.role_for(stage, complexity)
        if role is None:
            return None, [
                Refusal(
                    Reason.CONFIG_INVALID,
                    f"no role configured for stage={stage!r} complexity={complexity!r}",
                )
            ], notes
        spec = self.roles.get(role) or {}
        primary = spec.get("primary")
        if not primary:
            return None, [
                Refusal(Reason.CONFIG_INVALID, f"role {role!r} has no primary model")
            ], notes

        check = self.availability(primary)
        if check.available:
            return self._choice(role, primary, stage, None), [], notes

        notes.append(f"primary {primary} unavailable: {check.detail}")
        if not spec.get("allow_weaker_fallback", False):
            return None, [
                Refusal(
                    Reason.MODEL_UNAVAILABLE,
                    f"role {role}: primary {primary} is unavailable ({check.detail}) and "
                    "allow_weaker_fallback is false — refusing to substitute a weaker model",
                    {"role": role, "primary": primary, "detail": check.detail},
                )
            ], notes

        for fallback in spec.get("fallbacks") or []:
            fallback_check = self.availability(fallback)
            if fallback_check.available:
                notes.append(f"SUBSTITUTED {primary} -> {fallback}")
                return self._choice(role, fallback, stage, primary), [], notes
            notes.append(f"fallback {fallback} unavailable: {fallback_check.detail}")

        return None, [
            Refusal(
                Reason.MODEL_UNAVAILABLE,
                f"role {role}: neither {primary} nor any fallback is available",
                {"role": role, "notes": notes},
            )
        ], notes

    # ------------------------------------------------------------- diversity

    def diversity_check(self, complexity: str, choices: dict[str, ModelChoice]) -> Refusal | None:
        """Report a loss of independence for important reviews.

        This never blocks: the orchestrator's duty is to state plainly that plan,
        implementation and review were not independent, so a human can weigh it.
        """
        enforce = (self.config.get("diversity") or {}).get("enforce_for") or []
        if complexity not in enforce:
            return None
        families = {stage: choice.family for stage, choice in choices.items()}
        if len(set(families.values())) > 1:
            return None
        return Refusal(
            Reason.DIVERSITY_LOST,
            f"complexity={complexity}: plan/implement/review all come from the "
            f"{next(iter(families.values()))!r} family — this review is not independent",
            {"families": families},
        )
