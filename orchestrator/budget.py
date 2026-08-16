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
    REPORTED = "REPORTED"
    ESTIMATED = "ESTIMATED"
    FREE_LOCAL = "FREE_LOCAL"


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
            if entry.cost_source == str(CostSource.REPORTED):
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
