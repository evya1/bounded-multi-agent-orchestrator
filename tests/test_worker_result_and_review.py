"""The semantic layer: result schemas, review packets, and review routing.

The distinction under test throughout: a result block is what the worker SAYS.
It is never what ended the process, and it is never the verdict on the work.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from orchestrator import review_packet
from orchestrator.failures import Action, FailureClass, classify_review, policy_for
from orchestrator.worker_result import (
    REVIEWER_RESULT_CONTRACT,
    WORKER_RESULT_CONTRACT,
    ReviewerResult,
    parse_reviewer_result,
    parse_worker_result,
)
from orchestrator.workflow import Phase, next_after_review

GOOD = """
I edited two files.

```json
{"status": "done", "summary": "added the parser", "files_changed": ["src/a.py"],
 "tests_recommended": ["tests/test_a.py"], "needs_human": false}
```
"""


def test_a_valid_result_block_is_parsed():
    outcome = parse_worker_result(GOOD)

    assert outcome.ok
    assert outcome.result.status == "done"
    assert outcome.result.files_changed == ["src/a.py"]


def test_the_last_block_wins_over_an_example_quoted_earlier():
    """A prompt's example must never be mistaken for the worker's answer."""
    text = (
        "The contract said to emit:\n```json\n{\"status\": \"done\", \"summary\": \"EXAMPLE\"}\n```\n"
        "Here is my actual result:\n```json\n{\"status\": \"blocked\", \"summary\": \"REAL\"}\n```"
    )

    outcome = parse_worker_result(text)

    assert outcome.ok
    assert outcome.result.summary == "REAL"


def test_an_unfenced_object_is_still_found():
    outcome = parse_worker_result('Result: {"status": "failed", "summary": "nope"}')

    assert outcome.ok and outcome.result.status == "failed"


@pytest.mark.parametrize(
    "text",
    [
        "no json at all",
        '```json\n{"summary": "no status field"}\n```',
        '```json\n{"status": "done"\n```',          # unclosed
        '```json\n{"status": "finished"}\n```',      # not in the enum
    ],
    ids=["absent", "no-status", "malformed", "bad-enum"],
)
def test_a_malformed_result_is_reported_not_guessed(text):
    outcome = parse_worker_result(text)

    assert outcome.ok is False
    assert outcome.error
    assert outcome.result is None


def test_a_malformed_result_earns_exactly_one_bounded_repair():
    policy = policy_for(FailureClass.MALFORMED_RESULT)

    assert policy.action == Action.REPAIR_FORMAT
    assert policy.max_attempts == 1
    assert "never re-run the implementation" in policy.explanation


def test_a_wrong_typed_field_is_a_validation_error():
    outcome = parse_worker_result('```json\n{"status": "done", "files_changed": 5}\n```')

    assert outcome.ok is False
    assert "files_changed" in outcome.error


def test_the_worker_contract_asks_for_a_block_and_disclaims_its_authority():
    assert '"status": "done|blocked|failed"' in WORKER_RESULT_CONTRACT
    assert "It does not decide anything" in WORKER_RESULT_CONTRACT
    assert "determines the changed files from Git" in WORKER_RESULT_CONTRACT


# ------------------------------------------------------------------ reviewer


def test_a_reviewer_verdict_is_parsed_structurally_not_from_prose():
    text = 'I would APPROVE this.\n```json\n{"verdict": "changes_required", "blocking": ["bug"]}\n```'

    outcome = parse_reviewer_result(text)

    assert outcome.ok
    assert outcome.result.verdict == "changes_required"
    assert outcome.result.approved is False


def test_the_word_approve_in_prose_approves_nothing():
    outcome = parse_reviewer_result("APPROVE. Looks great to me.")

    assert outcome.ok is False


def test_the_reviewer_contract_is_a_verdict_not_an_edit():
    assert '"verdict": "approve|changes_required|blocked"' in REVIEWER_RESULT_CONTRACT


# ------------------------------------------------------------ review routing


def test_a_green_review_goes_straight_to_level_3_with_no_resolver():
    """The normal successful workflow invokes NO resolver and NO fixer."""
    phase, failure, _ = next_after_review(ReviewerResult(verdict="approve"), 0, 0)

    assert phase == Phase.LEVEL3
    assert failure == FailureClass.NONE


def test_a_simple_blocker_goes_to_the_cheap_fixer_not_the_resolver():
    result = ReviewerResult(verdict="changes_required", blocking=["off-by-one in the loop bound"])

    phase, failure, _ = next_after_review(result, 0, 0)

    assert phase == Phase.FIX
    assert failure == FailureClass.REVIEW_BLOCKER_SIMPLE


def test_an_architectural_disagreement_goes_to_the_resolver_once():
    result = ReviewerResult(
        verdict="blocked",
        blocking=["this is an architectural decision the task does not own"],
    )

    phase, failure, _ = next_after_review(result, 0, 0)

    assert phase == Phase.RESOLVE
    assert failure == FailureClass.ARCHITECTURAL_DISAGREEMENT


def test_the_resolver_is_never_invoked_twice():
    result = ReviewerResult(verdict="blocked", blocking=["ambiguous requirement in the contract"])

    phase, _, why = next_after_review(result, 0, resolver_cycles=1)

    assert phase == Phase.BLOCKED
    assert "human" in why


def test_a_second_fix_cycle_is_never_automatic():
    result = ReviewerResult(verdict="changes_required", blocking=["still wrong"])

    phase, _, why = next_after_review(result, fixer_cycles=1, resolver_cycles=0)

    assert phase == Phase.BLOCKED
    assert "human" in why


def test_an_approval_carrying_blocking_findings_is_not_treated_as_green():
    result = ReviewerResult(verdict="approve", blocking=["actually this crashes"])

    phase, _, _ = next_after_review(result, 0, 0)

    assert phase != Phase.LEVEL3


@pytest.mark.parametrize(
    ("finding", "expected"),
    [
        ("null dereference on line 12", FailureClass.REVIEW_BLOCKER_SIMPLE),
        ("the wrong abstraction is used here", FailureClass.ARCHITECTURAL_DISAGREEMENT),
        ("this contradicts the contract in docs/contracts/x.md", FailureClass.ARCHITECTURAL_DISAGREEMENT),
        ("missing test for the empty case", FailureClass.REVIEW_BLOCKER_SIMPLE),
    ],
)
def test_review_classification(finding, expected):
    assert classify_review([finding]) == expected


# ------------------------------------------------------------- review packet


class _Task:
    """A minimal stand-in for a loaded Task, with only the fields under test."""

    id = "T002"
    component = "reporting"
    task_type = "feature"
    risk = "medium"
    implements: ClassVar[list[str]] = ["REQ-1", "REQ-2"]
    write_set: ClassVar[list[str]] = ["src/reporting/"]


def test_a_review_packet_contains_only_orchestrator_derived_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(review_packet.gitio, "review_patch", lambda *a, **k: "--- a\n+++ b\n+x")

    packet = review_packet.build(
        repo="police",
        task=_Task(),
        worktree=tmp_path,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        requirements="REQ-1 MUST ...",
        validation_output="3 passed",
        write_set_result="1 path changed, inside the write set",
    )

    titles = [section.title for section in packet.sections]
    assert titles == [
        "TASK",
        "AUTHORITATIVE REQUIREMENTS",
        "ACCEPTANCE CRITERIA",
        "CANDIDATE DIFF",
        "RELEVANT SOURCE",
        "RELEVANT TESTS",
        "DETERMINISTIC TEST RESULTS",
        "SECURITY CHECKS",
        "DEPENDENCY DIFF",
        "WRITE-SET RESULT",
    ]


def test_a_review_packet_states_the_reviewer_has_no_repository(tmp_path, monkeypatch):
    monkeypatch.setattr(review_packet.gitio, "review_patch", lambda *a, **k: "diff")

    rendered = review_packet.build(
        repo="police", task=_Task(), worktree=tmp_path, base_sha="a", candidate_sha="b"
    ).render()

    assert "NO tools and NO repository access" in rendered
    assert "missing_evidence" in rendered


def test_an_oversized_section_is_truncated_visibly_never_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review_packet.gitio, "review_patch", lambda *a, **k: "x" * (review_packet.MAX_DIFF_BYTES + 5000)
    )

    packet = review_packet.build(
        repo="police", task=_Task(), worktree=tmp_path, base_sha="a", candidate_sha="b"
    )

    assert packet.truncated is True
    assert "TRUNCATED BY THE ORCHESTRATOR" in packet.render()
    assert any("truncated" in limitation for limitation in packet.known_limitations)


def test_the_packet_carries_both_shas_so_the_review_is_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(review_packet.gitio, "review_patch", lambda *a, **k: "diff")

    packet = review_packet.build(
        repo="police", task=_Task(), worktree=tmp_path, base_sha="a" * 40, candidate_sha="b" * 40
    )

    assert "a" * 40 in packet.render()
    assert "b" * 40 in packet.render()
