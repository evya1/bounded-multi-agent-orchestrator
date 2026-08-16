"""Read the project's own planning registers.

Gate resolution is DERIVED from the project artifacts, never guessed. Where a
register cannot mechanically answer "is this gate resolved?", the answer is
``UNKNOWN`` and the caller decides conservatively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .config import ProjectConfig

_REQ_ID = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]{3}$")
_ID_IN_TEXT = re.compile(r"\b(?:OPEN|PLANQ|INPUT)-[0-9]{3}\b")


class GateState(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    UNKNOWN = "UNKNOWN_GATE_STATE"


@dataclass(frozen=True)
class Excerpt:
    """A targeted slice of a register — one row, not the whole document."""

    id: str
    source: str
    line: int
    text: str
    cells: tuple[str, ...] = ()

    def cell(self, index: int) -> str:
        return self.cells[index] if index < len(self.cells) else ""


def _table_rows(text: str, heading: str | None = None) -> list[tuple[int, list[str]]]:
    """Yield (1-based line number, cells) for markdown table rows.

    When ``heading`` is given, only rows under that ``##`` heading are returned.
    """
    rows: list[tuple[int, list[str]]] = []
    active = heading is None
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("#"):
            active = heading is None or heading.lower() in line.lower()
            continue
        if not active or not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if cells and set("".join(cells)) <= set("-: "):
            continue  # separator row
        rows.append((number, cells))
    return rows


class Registers:
    """Lazy readers over one repository's planning registers."""

    def __init__(self, repo_path: Path, project: ProjectConfig) -> None:
        self.repo_path = repo_path
        self.project = project
        self._cache: dict[str, str] = {}

    def _text(self, relative: str) -> str:
        if relative not in self._cache:
            path = self.repo_path / relative
            self._cache[relative] = path.read_text(encoding="utf-8") if path.is_file() else ""
        return self._cache[relative]

    # ------------------------------------------------------------ requirements

    def requirement_ids(self) -> set[str]:
        text = self._text(self.project.requirement_register)
        return {
            cells[0]
            for _, cells in _table_rows(text)
            if cells and _REQ_ID.fullmatch(cells[0])
        }

    def requirement(self, requirement_id: str) -> Excerpt | None:
        """The single register row for one requirement ID — never the whole file."""
        source = self.project.requirement_register
        for number, cells in _table_rows(self._text(source)):
            if cells and cells[0] == requirement_id:
                return Excerpt(requirement_id, source, number, " | ".join(cells), tuple(cells))
        return None

    # ---------------------------------------------------------- OPEN / PLANQ

    def open_item(self, item_id: str) -> Excerpt | None:
        source = self.project.open_register
        heading = (
            self.project.decision_items_heading
            if item_id.startswith("PLANQ")
            else self.project.open_items_heading
        )
        for number, cells in _table_rows(self._text(source), heading):
            if cells and cells[0] == item_id:
                return Excerpt(item_id, source, number, " | ".join(cells), tuple(cells))
        return None

    def input_item(self, item_id: str) -> Excerpt | None:
        source = self.project.input_register
        for number, cells in _table_rows(self._text(source)):
            if cells and cells[0] == item_id:
                return Excerpt(item_id, source, number, " | ".join(cells), tuple(cells))
        return None

    def gate_class_ids(self) -> set[str]:
        """Named input-gate classes (G-OFFICIAL, G-TEAM, ...) defined in the register."""
        return set(re.findall(r"\bG-[A-Z]+\b", self._text(self.project.open_register)))

    def mentioned_ids(self, text: str) -> set[str]:
        """OPEN/PLANQ/INPUT IDs appearing in prose — reported, never auto-included."""
        return set(_ID_IN_TEXT.findall(text))

    # ------------------------------------------------------- gate resolution

    def gate_state(self, gate_id: str, kind: str) -> tuple[GateState, str]:
        """Derive a gate's resolution state from the project's own registers.

        Returns the state plus a short human explanation of how it was derived.
        """
        if kind == "decision":
            return self._decision_state(gate_id)
        if kind == "open":
            return self._open_state(gate_id)
        if kind == "input":
            return self._input_state(gate_id)
        if kind == "input_gate":
            return self._input_gate_state(gate_id)
        return (GateState.UNKNOWN, f"unrecognised gate kind {kind!r}")

    def _decision_state(self, gate_id: str) -> tuple[GateState, str]:
        """PLANQ-* rows carry a Decision cell; a TBD marker means undecided."""
        source = self.project.decision_register
        for _, cells in _table_rows(self._text(source), self.project.decision_items_heading):
            if not cells or cells[0] != gate_id:
                continue
            # Header: ID | question | constraints | Decision | Owner | Affected tasks
            if len(cells) < 4:
                return (GateState.UNKNOWN, f"{gate_id}: decision row has too few columns")
            decision = cells[3]
            if decision.upper() in {m.upper() for m in self.project.unresolved_decision_markers}:
                return (GateState.UNRESOLVED, f"{gate_id}: Decision is {decision!r}")
            return (GateState.RESOLVED, f"{gate_id}: Decision recorded as {decision!r}")
        return (GateState.UNKNOWN, f"{gate_id}: no row in {source} decision register")

    def _open_state(self, gate_id: str) -> tuple[GateState, str]:
        """A row still listed under 'Active OPEN items' is by definition unresolved."""
        if self.open_item(gate_id) is not None:
            return (
                GateState.UNRESOLVED,
                f"{gate_id}: still listed under {self.project.open_items_heading!r}",
            )
        return (GateState.UNKNOWN, f"{gate_id}: not found in the active OPEN register")

    def _input_state(self, gate_id: str) -> tuple[GateState, str]:
        """INPUT_REGISTER rows carry a Status cell (MISSING / EXPECTED / RECEIVED...)."""
        excerpt = self.input_item(gate_id)
        if excerpt is None:
            return (GateState.UNKNOWN, f"{gate_id}: no row in the input register")
        status = excerpt.cell(3)
        if status.upper() in {s.upper() for s in self.project.resolved_input_statuses}:
            return (GateState.RESOLVED, f"{gate_id}: input Status={status!r}")
        return (GateState.UNRESOLVED, f"{gate_id}: input Status={status!r}")

    def _input_gate_state(self, gate_id: str) -> tuple[GateState, str]:
        """Named gate CLASSES have no mechanical resolution marker.

        ``G-OFFICIAL`` is 'ready when Moodle/lecturer supplies the file' — that is
        a human judgement about many underlying items, not a parseable cell. We
        refuse to infer it.
        """
        if gate_id in self.gate_class_ids():
            return (
                GateState.UNKNOWN,
                f"{gate_id}: input-gate class has no mechanical resolution marker in "
                f"{self.project.open_register}; a human must resolve it",
            )
        return (GateState.UNKNOWN, f"{gate_id}: unknown input-gate class")
