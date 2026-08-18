"""The structured semantic result a bounded worker returns.

Two different questions, deliberately never conflated:

``LIFECYCLE``   Did the worker PROCESS finish? Answered by Pi's protocol, in
                ``pi_rpc``. A settled worker is finished even if it produced
                nonsense.
``SEMANTICS``   Did the worker DO the job? Answered here, by parsing and
                validating the machine-readable block it was asked to emit.

A malformed result is a semantic failure, not a lifecycle failure, and it earns
exactly ONE bounded format-repair attempt — re-running the whole implementation
because a model forgot a closing brace is how a cheap task becomes expensive.

Validation is hand-written against the stdlib. The project's only runtime
dependency is PyYAML; adding Pydantic to check eight fields would cost more than
it is worth.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum

#: The fenced block a worker is asked to end with. Recognising it is a parsing
#: convenience ONLY: its presence, absence or early appearance has no effect on
#: any process lifecycle. See ``pi_rpc`` for what actually ends a run.
RESULT_FENCE = "```json"
RESULT_MARKER = "ORCHESTRATOR_RESULT"

_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class Status(StrEnum):
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class WorkerResult:
    """What the worker says it did. Advice to the orchestrator, never a verdict."""

    status: str = str(Status.FAILED)
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    dependency_requests: list[str] = field(default_factory=list)
    tests_recommended: list[str] = field(default_factory=list)
    blocking_findings: list[str] = field(default_factory=list)
    non_blocking_findings: list[str] = field(default_factory=list)
    needs_human: bool = False

    @property
    def done(self) -> bool:
        return self.status == str(Status.DONE)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewerResult:
    """The reviewer's structured verdict. The reviewer cannot edit anything."""

    verdict: str = "blocked"
    blocking: list[str] = field(default_factory=list)
    non_blocking: list[str] = field(default_factory=list)
    informational: list[str] = field(default_factory=list)
    requirements_checked: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ParseOutcome:
    """Either a validated result, or the exact reason it could not be produced."""

    ok: bool
    result: WorkerResult | ReviewerResult | None = None
    error: str = ""
    raw_block: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "result": self.result.as_dict() if self.result else None,
        }


def _candidates(text: str) -> list[str]:
    """Every plausible JSON object in the text, LAST first.

    Last first because the contract asks for the result block at the END. An
    example object quoted earlier in the prompt or the prose must not win over
    the worker's actual final answer.
    """
    blocks = _FENCED.findall(text or "")
    if blocks:
        return list(reversed(blocks))
    # Fall back to the last balanced top-level object in the text.
    found: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text or ""):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                found.append(text[start : index + 1])
                start = -1
    return list(reversed(found))


def _as_str_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) if not isinstance(item, str) else item for item in value]
    raise ValueError(f"{field_name!r} must be a list of strings, got {type(value).__name__}")


def parse_worker_result(text: str) -> ParseOutcome:
    """Validate an implementer/fixer result block. Never trusts prose."""
    for block in _candidates(text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "status" not in data:
            continue
        status = str(data.get("status", "")).strip().lower()
        if status not in {str(s) for s in Status}:
            return ParseOutcome(
                False,
                None,
                f"status must be one of done|blocked|failed, got {status!r}",
                block,
            )
        try:
            result = WorkerResult(
                status=status,
                summary=str(data.get("summary", "")),
                files_changed=_as_str_list(data.get("files_changed"), "files_changed"),
                dependency_requests=_as_str_list(
                    data.get("dependency_requests"), "dependency_requests"
                ),
                tests_recommended=_as_str_list(data.get("tests_recommended"), "tests_recommended"),
                blocking_findings=_as_str_list(data.get("blocking_findings"), "blocking_findings"),
                non_blocking_findings=_as_str_list(
                    data.get("non_blocking_findings"), "non_blocking_findings"
                ),
                needs_human=bool(data.get("needs_human", False)),
            )
        except ValueError as exc:
            return ParseOutcome(False, None, str(exc), block)
        return ParseOutcome(True, result, "", block)
    return ParseOutcome(False, None, "no valid result object with a 'status' field was found", "")


def parse_reviewer_result(text: str) -> ParseOutcome:
    """Validate a reviewer verdict block."""
    allowed = {"approve", "changes_required", "blocked"}
    for block in _candidates(text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "verdict" not in data:
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in allowed:
            return ParseOutcome(
                False, None, f"verdict must be one of {sorted(allowed)}, got {verdict!r}", block
            )
        try:
            result = ReviewerResult(
                verdict=verdict,
                blocking=_as_str_list(data.get("blocking"), "blocking"),
                non_blocking=_as_str_list(data.get("non_blocking"), "non_blocking"),
                informational=_as_str_list(data.get("informational"), "informational"),
                requirements_checked=_as_str_list(
                    data.get("requirements_checked"), "requirements_checked"
                ),
                missing_evidence=_as_str_list(data.get("missing_evidence"), "missing_evidence"),
            )
        except ValueError as exc:
            return ParseOutcome(False, None, str(exc), block)
        return ParseOutcome(True, result, "", block)
    return ParseOutcome(False, None, "no valid result object with a 'verdict' field was found", "")


WORKER_RESULT_CONTRACT = """\
## Final result block (required)

End your reply with ONE fenced JSON block, and nothing after it:

```json
{
  "status": "done|blocked|failed",
  "summary": "one or two sentences",
  "files_changed": [],
  "dependency_requests": [],
  "tests_recommended": [],
  "blocking_findings": [],
  "non_blocking_findings": [],
  "needs_human": false
}
```

This block reports what you BELIEVE you did. It does not decide anything: the
orchestrator determines the changed files from Git, and the test suite decides
whether the work passed. Reporting "done" for work that does not compile makes
the report wrong, not the work correct.
"""

REVIEWER_RESULT_CONTRACT = """\
## Final result block (required)

End your reply with ONE fenced JSON block, and nothing after it:

```json
{
  "verdict": "approve|changes_required|blocked",
  "blocking": [],
  "non_blocking": [],
  "informational": [],
  "requirements_checked": [],
  "missing_evidence": []
}
```

Put a finding in `blocking` only if it must be fixed before merge. Put a
disagreement about architecture or an ambiguous requirement in `blocking` AND
say so in the text — those route to a resolver, not to a code fix.
"""

REPAIR_PROMPT = """\
Your previous reply did not contain a valid result block.

Parser error: {error}

Reply with ONLY the fenced JSON result block described below. No explanation,
no preamble, no code — just the block. Do not redo any work.

{contract}
"""
