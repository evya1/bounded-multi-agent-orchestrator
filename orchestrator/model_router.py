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
from dataclasses import dataclass, field, replace
from enum import StrEnum
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


class Presence(StrEnum):
    """Three genuinely different things, which the old boolean conflated.

    ``CONFIGURED``    this orchestrator's routing table names the model;
    ``REGISTERED``    the runtime (Pi's catalog, or the local provider
                      extension) knows the model exists;
    ``AVAILABLE_NOW`` a health check just succeeded, so work may be sent to it.

    A local model whose GPU server is stopped is REGISTERED and NOT available.
    Treating registration as availability is exactly how work gets dispatched
    into a connection refused.
    """

    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED = "CONFIGURED"
    REGISTERED = "REGISTERED"
    AVAILABLE_NOW = "AVAILABLE_NOW"


@dataclass
class Availability:
    """Whether one configured model can actually be reached right now."""

    name: str
    available: bool
    detail: str
    presence: str = str(Presence.UNCONFIGURED)

    def as_dict(self) -> dict:
        return {
            "model": self.name,
            "available": self.available,
            "detail": self.detail,
            "presence": self.presence,
        }


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

    def base_url_for(self, model_name: str) -> str:
        """The health-check endpoint for one local model.

        Each local model is served by its OWN llama.cpp process on its own port,
        so the URL belongs to the MODEL, not to the provider. A provider-level
        URL would report the one running server as proof that all four are up.
        """
        model = self.models.get(model_name) or {}
        provider = self.providers.get(model.get("provider", "")) or {}
        override = model.get("base_url_env") or provider.get("base_url_env") or ""
        if override and self.environ.get(override):
            return str(self.environ[override])
        return str(model.get("base_url") or provider.get("default_base_url") or "")

    def availability(self, model_name: str) -> Availability:
        """Can this model be reached, without spending anything to find out?

        Nothing here makes a paid call. Cloud availability is read from Pi's
        local non-secret catalog plus credential presence; local availability is
        an unauthenticated HTTP probe of the model's own server.
        """
        model = self.models.get(model_name)
        if model is None:
            return Availability(
                model_name, False, "not defined in the routing table", str(Presence.UNCONFIGURED)
            )
        provider_name = model.get("provider", "")
        provider = self.providers.get(provider_name) or {}

        if provider.get("kind") == "local":
            model_id = model.get("id")
            if not model_id:
                return Availability(
                    model_name,
                    False,
                    f"{model_name}: no local model id configured",
                    str(Presence.CONFIGURED),
                )
            base_url = self.base_url_for(model_name)
            if not base_url:
                return Availability(
                    model_name,
                    False,
                    f"{model_id}: registered but no base URL configured",
                    str(Presence.REGISTERED),
                )
            ok, detail = _probe_http(base_url.rstrip("/") + "/v1/models")
            if ok:
                return Availability(
                    model_name,
                    True,
                    f"{model_id} @ {base_url}: {detail}",
                    str(Presence.AVAILABLE_NOW),
                )
            # REGISTERED in Pi, but its server is not answering. Registration is
            # not availability, and this is the exact case that must not route.
            return Availability(
                model_name,
                False,
                f"{model_id} @ {base_url}: SERVER OFFLINE ({detail}) — registered in Pi but "
                "not running; start it externally, this orchestrator does not manage GPU servers",
                str(Presence.REGISTERED),
            )

        if not self.key_available(provider_name):
            return Availability(
                model_name,
                False,
                f"{provider_name}: credentials not configured",
                str(Presence.CONFIGURED),
            )
        model_id = model.get("id")
        catalog = self.catalog()
        if catalog and model_id not in catalog:
            return Availability(
                model_name,
                False,
                f"{model_id}: absent from the {provider_name} catalog",
                str(Presence.CONFIGURED),
            )
        return Availability(
            model_name,
            True,
            f"{model_id}: present in the {provider_name} catalog",
            str(Presence.AVAILABLE_NOW),
        )

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

        primary_family = str((self.models.get(primary) or {}).get("family", "unknown"))
        for fallback in spec.get("fallbacks") or []:
            fallback_family = str((self.models.get(fallback) or {}).get("family", "unknown"))
            if fallback_family != primary_family and not spec.get("allow_family_substitution", False):
                # A different family is a different reviewer, a different price
                # and a different failure mode. It is never an automatic choice.
                notes.append(
                    f"fallback {fallback} REFUSED: family {fallback_family!r} != primary family "
                    f"{primary_family!r} and allow_family_substitution is not set"
                )
                continue
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

    # ---------------------------------------------------------- role budgets

    def limits_for(self, role: str):
        """The bounded-run limits for one role, from ``roles.<role>.limits``.

        Imported lazily: ``pi_rpc`` owns the limit dataclass, and the router must
        not become the place where limits are also *defined*.
        """
        from .pi_rpc import RoleLimits

        spec = (self.roles.get(role) or {}).get("limits")
        limits = RoleLimits.from_config(spec)
        primary = (self.roles.get(role) or {}).get("primary")
        model = self.models.get(str(primary)) or {}
        provider = self.providers.get(str(model.get("provider", ""))) or {}
        if not provider.get("paid", True):
            # A local model's EXTERNAL API ceiling is zero by construction. Its
            # turn, tool, wall-time and retry limits are what actually bound it.
            limits = replace(limits, soft_usd=0.0, hard_usd=0.0)
        return limits

    def is_paid(self, role: str) -> bool:
        primary = (self.roles.get(role) or {}).get("primary")
        model = self.models.get(str(primary)) or {}
        provider = self.providers.get(str(model.get("provider", ""))) or {}
        return bool(provider.get("paid", True))

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


# --------------------------------------------------------- no-Claude guarantee

#: Every spelling of "this is an Anthropic model" that could appear in a routing
#: table. Matched case-insensitively against the model name, its provider slug
#: and its declared family.
CLAUDE_MARKERS = ("anthropic", "claude", "opus", "sonnet", "haiku", "fable")

#: Every place a runtime route can hide. `resolve` reads all of them, so the
#: validator must too — a Claude model reachable only through an unknown-role
#: default would still be a runtime Claude route.
ROUTE_KEYS = ("primary", "fallbacks", "default", "default_model", "on_unavailable")


def _looks_anthropic(*values: object) -> bool:
    for value in values:
        text = str(value or "").lower()
        if any(marker in text for marker in CLAUDE_MARKERS):
            return True
    return False


def claude_runtime_routes(models_config: dict) -> list[Refusal]:
    """Every ACTIVE runtime route that could reach an Anthropic/Claude model.

    This is a configuration-validation function, not a text search. It reports
    only routes the runtime could actually take:

    * a model definition an enabled role points at;
    * a role primary, a role fallback, or any role-level default;
    * an enabled adapter whose executable is the Claude CLI.

    Documentation, comments and historical notes that merely MENTION Claude are
    deliberately not findings. The requirement is that no active route exists,
    not that the word is unspeakable.
    """
    refusals: list[Refusal] = []
    models = models_config.get("models") or {}
    providers = models_config.get("providers") or {}
    roles = models_config.get("roles") or {}

    def flag(where: str, model_name: str, model_spec: dict) -> None:
        refusals.append(
            Refusal(
                Reason.CONFIG_INVALID,
                f"{where}: {model_name!r} routes to an Anthropic/Claude model "
                f"(id={model_spec.get('id')!r}, family={model_spec.get('family')!r}); "
                "this orchestrator has no Claude runtime route by design",
                {"where": where, "model": model_name, "id": model_spec.get("id")},
            )
        )

    def anthropic_model(model_name: str) -> dict | None:
        spec = models.get(model_name)
        if spec is None:
            return None
        provider = providers.get(str(spec.get("provider", ""))) or {}
        if _looks_anthropic(
            model_name, spec.get("id"), spec.get("family"), provider.get("pi_provider")
        ):
            return spec
        return None

    for role_name, role_spec in roles.items():
        if not isinstance(role_spec, dict):
            continue
        for key in ROUTE_KEYS:
            value = role_spec.get(key)
            targets = value if isinstance(value, list) else ([value] if value else [])
            for target in targets:
                spec = anthropic_model(str(target))
                if spec is not None:
                    flag(f"roles.{role_name}.{key}", str(target), spec)

    for key in ROUTE_KEYS:
        value = models_config.get(key)
        targets = value if isinstance(value, list) else ([value] if value else [])
        for target in targets:
            spec = anthropic_model(str(target))
            if spec is not None:
                flag(f"models.{key}", str(target), spec)

    for adapter_name, adapter_spec in (models_config.get("adapters") or {}).items():
        if not isinstance(adapter_spec, dict) or not adapter_spec.get("enabled", False):
            continue
        if _looks_anthropic(adapter_name, adapter_spec.get("executable")):
            refusals.append(
                Refusal(
                    Reason.CONFIG_INVALID,
                    f"adapters.{adapter_name} is ENABLED and executes "
                    f"{adapter_spec.get('executable')!r}; Claude may not be a runtime worker",
                    {"adapter": adapter_name},
                )
            )
    return refusals


def validate_no_claude_runtime(models_config: dict) -> list[Refusal]:
    """Raise-free assertion that the active configuration cannot route to Claude."""
    return claude_runtime_routes(models_config)
