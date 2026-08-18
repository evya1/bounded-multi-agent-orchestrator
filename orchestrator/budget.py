"""Cost governance.

A hard daily cap with a reserve, a per-call ceiling, and an append-only ledger.

Cost provenance is labelled, never laundered:

``REPORTED``   Pi returned per-call usage for this run. Token counts are the
              provider's; the dollar figure is Pi's own catalog arithmetic.
``ESTIMATED``  We priced it ourselves from catalog metadata and a token guess.

Neither is an OpenRouter invoice. The ledger says which one it is on every row,
so a total is never mistaken for authoritative billing.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from .config import BudgetConfig, Config
from .errors import Reason, Refusal


class CostSource(StrEnum):
    """Where a dollar figure came from. Recorded on every row, never laundered.

    ``provider_reported`` Pi returned per-call usage from the provider.
    ``catalog_estimate``  we priced it locally from catalog metadata.
    ``local_zero``        local inference: zero EXTERNAL API spend. This says
                          nothing about the electricity or the GPU; it says this
                          call put nothing on an OpenRouter invoice.
    """

    REPORTED = "provider_reported"
    ESTIMATED = "catalog_estimate"
    FREE_LOCAL = "local_zero"


#: Values written by earlier versions of the ledger, so an existing file still
#: totals correctly instead of silently reclassifying old spend as estimated.
_LEGACY_SOURCES = {
    "REPORTED": CostSource.REPORTED,
    "ESTIMATED": CostSource.ESTIMATED,
    "FREE_LOCAL": CostSource.FREE_LOCAL,
}


def normalise_source(value: str) -> str:
    return str(_LEGACY_SOURCES.get(value, value))


@dataclass
class LedgerEntry:
    """One recorded model invocation."""

    at: str
    day: str
    repo: str
    task_id: str
    stage: str
    role: str
    provider: str
    model: str
    paid: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cost_source: str = str(CostSource.ESTIMATED)
    duration_s: float = 0.0
    exit_code: int = 0
    note: str = ""
    #: Run identity, so per-role and per-run budgets are computable from the
    #: ledger alone rather than from something the caller remembers.
    task_run_id: str = ""
    worker_id: str = ""
    reasoning_tokens: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BudgetStatus:
    day: str
    limit_usd: float
    reserve_usd: float
    spent_usd: float
    max_usd_per_call: float
    by_model: dict[str, float] = field(default_factory=dict)
    by_role: dict[str, float] = field(default_factory=dict)
    by_task: dict[str, float] = field(default_factory=dict)
    reported_usd: float = 0.0
    estimated_usd: float = 0.0
    calls: int = 0

    @property
    def spendable_usd(self) -> float:
        return max(0.0, self.limit_usd - self.reserve_usd - self.spent_usd)

    @property
    def remaining_usd(self) -> float:
        return self.limit_usd - self.spent_usd

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "spendable_usd": round(self.spendable_usd, 4),
            "remaining_usd": round(self.remaining_usd, 4),
        }


def _today() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def estimate_cost(
    price: dict, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
) -> float:
    """Price a call from catalog metadata (USD per million tokens)."""
    million = 1_000_000
    return (
        input_tokens * float(price.get("input", 0.0))
        + output_tokens * float(price.get("output", 0.0))
        + cache_read_tokens * float(price.get("cache_read", 0.0))
    ) / million


class Ledger:
    """Append-only JSONL spend ledger. Never records prompts or secrets."""

    def __init__(self, config: Config) -> None:
        self.path = config.state_dir / "ledger.jsonl"
        self.budget: BudgetConfig = config.budget

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.as_dict(), sort_keys=True) + "\n")
        return entry

    def entries(self, day: str | None = None) -> list[LedgerEntry]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = LedgerEntry(**json.loads(line))
            if day is None or entry.day == day:
                rows.append(entry)
        return rows

    def status(self, day: str | None = None) -> BudgetStatus:
        target = day or _today()
        status = BudgetStatus(
            day=target,
            limit_usd=self.budget.daily_openrouter_budget_usd,
            reserve_usd=self.budget.reserve_usd,
            max_usd_per_call=self.budget.max_usd_per_call,
            spent_usd=0.0,
        )
        for entry in self.entries(target):
            status.calls += 1
            if not entry.paid:
                continue
            status.spent_usd += entry.cost_usd
            status.by_model[entry.model] = status.by_model.get(entry.model, 0.0) + entry.cost_usd
            status.by_role[entry.role] = status.by_role.get(entry.role, 0.0) + entry.cost_usd
            key = f"{entry.repo}/{entry.task_id}"
            status.by_task[key] = status.by_task.get(key, 0.0) + entry.cost_usd
            if normalise_source(entry.cost_source) == str(CostSource.REPORTED):
                status.reported_usd += entry.cost_usd
            else:
                status.estimated_usd += entry.cost_usd
        status.spent_usd = round(status.spent_usd, 6)
        return status

    def authorize(self, paid: bool, estimated_usd: float, day: str | None = None) -> Refusal | None:
        """Refuse BEFORE a paid call that would breach the per-call or daily cap."""
        if not paid:
            return None
        status = self.status(day)
        if estimated_usd > self.budget.max_usd_per_call:
            return Refusal(
                Reason.BUDGET_REFUSED,
                f"estimated ${estimated_usd:.4f} exceeds max_usd_per_call "
                f"${self.budget.max_usd_per_call:.2f}",
                {"estimated_usd": estimated_usd, "cap": self.budget.max_usd_per_call},
            )
        if estimated_usd > status.spendable_usd:
            return Refusal(
                Reason.BUDGET_REFUSED,
                f"estimated ${estimated_usd:.4f} exceeds today's spendable "
                f"${status.spendable_usd:.4f} (limit ${status.limit_usd:.2f}, "
                f"spent ${status.spent_usd:.4f}, reserve ${status.reserve_usd:.2f})",
                {"estimated_usd": estimated_usd, "spendable_usd": status.spendable_usd},
            )
        return None


@dataclass(frozen=True)
class Envelope:
    """One soft/hard pair. Soft warns; hard forbids. Nothing escalates silently."""

    label: str
    soft_usd: float
    hard_usd: float

    def as_dict(self) -> dict:
        return {"label": self.label, "soft_usd": self.soft_usd, "hard_usd": self.hard_usd}


@dataclass
class SpendVerdict:
    """The answer to 'may this worker make one more paid generation?'"""

    allowed: bool
    refusal: Refusal | None = None
    warnings: list[str] = field(default_factory=list)
    checked: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "refusal": self.refusal.as_dict() if self.refusal else None,
            "warnings": self.warnings,
            "checked": self.checked,
        }


class RunBudget:
    """Budgets as first-class state, at three scopes at once.

    A worker, its role and the whole task run each carry an expected cost, a
    soft budget and a hard budget. Before EVERY paid generation all three are
    checked plus the daily cap, and the check asks the forward-looking question:
    could ONE more call of this size cross a hard line? If yes, the call is not
    made. There is no automatic expensive fallback and no hidden escalation —
    the only way past a hard budget is a human raising it.
    """

    def __init__(self, ledger: Ledger, task_run_id: str, total: Envelope) -> None:
        self.ledger = ledger
        self.task_run_id = task_run_id
        self.total = total
        self.roles: dict[str, Envelope] = {}

    def set_role(self, role: str, envelope: Envelope) -> None:
        self.roles[role] = envelope

    def spent_on_run(self) -> float:
        return round(
            sum(
                entry.cost_usd
                for entry in self.ledger.entries()
                if entry.paid and entry.task_run_id == self.task_run_id
            ),
            6,
        )

    def spent_on_role(self, role: str) -> float:
        return round(
            sum(
                entry.cost_usd
                for entry in self.ledger.entries()
                if entry.paid and entry.task_run_id == self.task_run_id and entry.role == role
            ),
            6,
        )

    def authorize_generation(
        self, role: str, expected_usd: float, paid: bool = True, day: str | None = None
    ) -> SpendVerdict:
        """May one more paid generation of ``expected_usd`` be dispatched?"""
        verdict = SpendVerdict(allowed=True)
        if not paid:
            verdict.checked.append({"scope": "local", "detail": "zero external API spend"})
            return verdict

        role_envelope = self.roles.get(role)
        scopes: list[tuple[str, Envelope, float]] = []
        if role_envelope is not None:
            scopes.append((f"role:{role}", role_envelope, self.spent_on_role(role)))
        scopes.append(("run", self.total, self.spent_on_run()))

        for name, envelope, spent in scopes:
            projected = spent + expected_usd
            verdict.checked.append(
                {
                    "scope": name,
                    "spent_usd": round(spent, 6),
                    "expected_usd": round(expected_usd, 6),
                    "projected_usd": round(projected, 6),
                    "soft_usd": envelope.soft_usd,
                    "hard_usd": envelope.hard_usd,
                }
            )
            if envelope.hard_usd > 0 and projected > envelope.hard_usd:
                verdict.allowed = False
                verdict.refusal = Refusal(
                    Reason.BUDGET_REFUSED,
                    f"{name}: one more generation would take spend to "
                    f"${projected:.4f}, past the HARD budget ${envelope.hard_usd:.4f}. "
                    "No call was made. Raising a hard budget is a human decision.",
                    {
                        "scope": name,
                        "spent_usd": spent,
                        "expected_usd": expected_usd,
                        "hard_usd": envelope.hard_usd,
                    },
                )
                return verdict
            if envelope.soft_usd > 0 and projected > envelope.soft_usd:
                verdict.warnings.append(
                    f"{name}: projected ${projected:.4f} is past the SOFT budget "
                    f"${envelope.soft_usd:.4f} (proceeding; the hard budget is "
                    f"${envelope.hard_usd:.4f})"
                )

        daily = self.ledger.authorize(True, expected_usd, day)
        if daily is not None:
            verdict.allowed = False
            verdict.refusal = daily
        return verdict

    def as_dict(self) -> dict:
        return {
            "task_run_id": self.task_run_id,
            "total": {**self.total.as_dict(), "spent_usd": self.spent_on_run()},
            "roles": {
                role: {**envelope.as_dict(), "spent_usd": self.spent_on_role(role)}
                for role, envelope in sorted(self.roles.items())
            },
        }


def envelopes_from_roles(roles_config: dict) -> dict[str, Envelope]:
    """Read ``roles.<name>.limits.{soft_usd,hard_usd}`` into budget envelopes."""
    envelopes: dict[str, Envelope] = {}
    for role, spec in (roles_config or {}).items():
        limits = (spec or {}).get("limits") or {}
        envelopes[role] = Envelope(
            label=role,
            soft_usd=float(limits.get("soft_usd", 0.0) or 0.0),
            hard_usd=float(limits.get("hard_usd", 0.0) or 0.0),
        )
    return envelopes


def render_status(status: BudgetStatus) -> str:
    lines = [
        f"BUDGET  day={status.day}",
        f"  limit      ${status.limit_usd:.2f}",
        f"  spent      ${status.spent_usd:.4f}  ({status.calls} recorded call(s))",
        f"  reserve    ${status.reserve_usd:.2f}",
        f"  remaining  ${status.remaining_usd:.4f}",
        f"  spendable  ${status.spendable_usd:.4f}   (remaining minus reserve)",
        f"  per-call   ${status.max_usd_per_call:.2f}",
        f"  provenance REPORTED ${status.reported_usd:.4f} / ESTIMATED ${status.estimated_usd:.4f}",
        "",
    ]
    for title, mapping in (
        ("by model", status.by_model),
        ("by role", status.by_role),
        ("by repo/task", status.by_task),
    ):
        lines.append(f"  {title}:")
        if not mapping:
            lines.append("    (nothing recorded)")
        for key, value in sorted(mapping.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {key:<40} ${value:.4f}")
    lines.append("")
    lines.append(
        "  NOTE: REPORTED = Pi's per-call usage (provider token counts, Pi catalog pricing)."
    )
    lines.append("        ESTIMATED = priced locally from catalog metadata. Neither is an invoice.")
    return "\n".join(lines)


def ledger_path(config: Config) -> Path:
    return config.state_dir / "ledger.jsonl"
