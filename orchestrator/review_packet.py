"""Deterministic review packets.

An expensive reviewer that can browse a repository will browse a repository.
Every tool call it makes is another paid generation, and the cost of a review
stops being predictable the moment the model chooses its own inputs.

So the reviewer gets no tools and no repository. It gets a packet: a fixed set
of sections, assembled by Python from Git and from the deterministic gates that
already ran. Everything in it is a fact the orchestrator can prove — the base
SHA, the candidate SHA, the actual patch, the actual test output, the actual
write-set audit. The reviewer's job is to judge that evidence, once.

The packet is persisted as a run artifact, so "what was the reviewer actually
shown?" is answerable later without re-running anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gitio
from .task_loader import Task

#: Hard ceilings on what one packet may contain. A packet that would exceed
#: them is TRUNCATED with an explicit marker rather than silently trimmed:
#: a reviewer must know it is looking at part of a change.
MAX_DIFF_BYTES = 200_000
MAX_SOURCE_BYTES = 120_000
MAX_TEST_OUTPUT_BYTES = 20_000


@dataclass
class PacketSection:
    title: str
    body: str
    truncated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewPacket:
    """Exactly what an expensive reviewer is shown. Nothing else."""

    repo: str
    task_id: str
    base_sha: str
    candidate_sha: str
    sections: list[PacketSection] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    known_limitations: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(len(section.body.encode()) for section in self.sections)

    @property
    def truncated(self) -> bool:
        return any(section.truncated for section in self.sections)

    def as_dict(self) -> dict:
        return {
            "repo": self.repo,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "candidate_sha": self.candidate_sha,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "section_titles": [section.title for section in self.sections],
            "sections": [section.as_dict() for section in self.sections],
            "questions": self.questions,
            "known_limitations": self.known_limitations,
        }

    def render(self) -> str:
        parts = [
            f"# REVIEW PACKET — {self.repo}/{self.task_id}",
            "",
            f"BASE SHA:      {self.base_sha}",
            f"CANDIDATE SHA: {self.candidate_sha}",
            "",
            "You have NO tools and NO repository access. Everything you may consider is",
            "below. If the evidence here is insufficient to judge a requirement, say so in",
            "`missing_evidence` rather than assuming.",
            "",
        ]
        for section in self.sections:
            parts += [f"## {section.title}", ""]
            parts.append(section.body if section.body.strip() else "(nothing)")
            if section.truncated:
                parts.append("\n[TRUNCATED BY THE ORCHESTRATOR — this section is incomplete]")
            parts.append("")
        if self.known_limitations:
            parts += ["## KNOWN LIMITATIONS", ""]
            parts += [f"- {item}" for item in self.known_limitations]
            parts.append("")
        if self.questions:
            parts += ["## QUESTIONS TO REVIEWER", ""]
            parts += [f"{index}. {question}" for index, question in enumerate(self.questions, 1)]
            parts.append("")
        return "\n".join(parts)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    encoded = (text or "").encode()
    if len(encoded) <= limit:
        return text or "", False
    return encoded[:limit].decode("utf-8", "ignore"), True


def build(
    *,
    repo: str,
    task: Task,
    worktree: Path,
    base_sha: str,
    candidate_sha: str,
    requirements: str = "",
    acceptance_criteria: str = "",
    relevant_source: str = "",
    relevant_tests: str = "",
    validation_output: str = "",
    security_output: str = "",
    dependency_diff: str = "",
    write_set_result: str = "",
    known_limitations: list[str] | None = None,
    questions: list[str] | None = None,
    ignore_prefixes: tuple[str, ...] = (".agent/",),
) -> ReviewPacket:
    """Assemble a packet from proven facts. Every argument is orchestrator-derived."""
    diff, diff_truncated = _clip(
        gitio.review_patch(worktree, base_sha, ignore_prefixes), MAX_DIFF_BYTES
    )
    source, source_truncated = _clip(relevant_source, MAX_SOURCE_BYTES)
    tests, tests_truncated = _clip(relevant_tests, MAX_SOURCE_BYTES)
    validation, validation_truncated = _clip(validation_output, MAX_TEST_OUTPUT_BYTES)

    packet = ReviewPacket(
        repo=repo,
        task_id=task.id,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        known_limitations=list(known_limitations or []),
        questions=list(
            questions
            or [
                "Which requirement IDs in `implements` are NOT actually satisfied by this diff?",
                "Which blocking findings are implementation bugs a cheap fixer can repair?",
                "Which blocking findings are architectural or requirement disagreements?",
            ]
        ),
    )
    packet.sections = [
        PacketSection("TASK", _task_block(task)),
        PacketSection("AUTHORITATIVE REQUIREMENTS", requirements),
        PacketSection("ACCEPTANCE CRITERIA", acceptance_criteria),
        PacketSection("CANDIDATE DIFF", diff, diff_truncated),
        PacketSection("RELEVANT SOURCE", source, source_truncated),
        PacketSection("RELEVANT TESTS", tests, tests_truncated),
        PacketSection("DETERMINISTIC TEST RESULTS", validation, validation_truncated),
        PacketSection("SECURITY CHECKS", security_output),
        PacketSection("DEPENDENCY DIFF", dependency_diff),
        PacketSection("WRITE-SET RESULT", write_set_result),
    ]
    if packet.truncated:
        packet.known_limitations.append(
            "One or more sections exceeded the packet size ceiling and were truncated."
        )
    return packet


def _task_block(task: Task) -> str:
    return "\n".join(
        [
            f"Task ID:      {task.id}",
            f"Component:    {task.component}",
            f"Type:         {task.task_type}",
            f"Risk:         {task.risk}",
            f"Implements:   {', '.join(task.implements) or 'none'}",
            "Write set:",
            *(f"  {path}" for path in task.write_set or ["(empty)"]),
        ]
    )
