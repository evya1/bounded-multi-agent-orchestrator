"""Worker prompts.

Every prompt carries the same stop-and-escalate contract. A bounded worker that
meets something outside its bounds must return ``STOP_NEEDS_ORCHESTRATOR``
rather than improvise: improvisation is how a task silently acquires a new
architectural decision, a widened write set, or someone else's dependency file.

The prompt is a *statement of the boundary*, not the enforcement of it. The
enforcement is the adapter's argv (no tools for read-only roles) and the
deterministic Git audit afterwards.
"""

from __future__ import annotations

from .context_compiler import ContextManifest
from .task_loader import Task

STOP_CONTRACT = """\
## Stop-and-escalate contract (binding)

You are executing a BOUNDED task. You have been given exactly the context the
orchestrator determined this task needs, and nothing else. That is deliberate.

If completing this task would require any of the following, DO NOT improvise:

  - a new architectural decision;
  - a new requirement, or widening an existing one;
  - writing outside the declared write set;
  - reading broad undeclared project context;
  - resolving an OPEN-* / PLANQ-* decision that is not already approved;
  - modifying a global dependency artifact you do not own.

Instead, stop immediately and return exactly this token on its own line:

    STOP_NEEDS_ORCHESTRATOR

followed by three short sections:

    WHAT IS MISSING:   the specific artifact, decision or permission
    WHY IT IS NEEDED:  what in the task requires it
    SMALLEST NEXT STEP: the minimum action that would unblock you

An honest stop is a successful outcome. A guess is not.
"""

_ROLE_RULES = {
    "plan": """\
## Your role: PLANNER (read-only)

You have NO tools. Write no code and edit no file.

Produce a numbered implementation plan for this task that a weaker model can
follow literally, leaving no design decision open. For each step give the exact
file, the exact signature, and the exact assertion its test must make. Every
requirement ID listed in `implements` must map to at least one step.

Confine every step to the declared write set. If the task is under-specified,
list the open questions instead of resolving them yourself.

Output markdown only.""",
    "implement": """\
## Your role: IMPLEMENTER

Follow the approved plan literally. Make no design decisions of your own.

You may edit ONLY the files in the declared write set. You have no shell.
Do NOT run tests, linters or any verification command — the orchestrator runs
those itself and decides whether they passed. Your job is to edit; proving the
code works is not delegated to you.

If a plan step is ambiguous, stop rather than guess.""",
    "review": """\
## Your role: REVIEWER (read-only)

You have NO tools. Write no code and edit no file.

Review the change below against the task's acceptance criteria and every
requirement ID in `implements`. Report:

  1. requirement IDs not actually satisfied;
  2. constraint violations;
  3. tests that assert nothing meaningful;
  4. anything written outside the declared write set.

End your response with exactly one line: APPROVE or REJECT.

Your verdict is advice to a human, not an approval. It does not ship anything.""",
}


def _write_set_block(task: Task) -> str:
    if not task.write_set:
        return "  (empty — this task may not write any file)"
    return "\n".join(f"  {path}" for path in task.write_set)


def build_prompt(
    stage: str,
    task: Task,
    manifest: ContextManifest,
    plan: str | None = None,
    change_summary: str | None = None,
) -> str:
    """Assemble the full bounded prompt for one stage."""
    if stage not in _ROLE_RULES:
        raise ValueError(f"unknown stage {stage!r}")

    sections = [
        f"# Task {task.id} — repository `{manifest.repo}` (stage: {stage})",
        "",
        f"Base commit: {manifest.base_sha}",
        f"Risk: {task.risk}    Component: {task.component}    Type: {task.task_type}",
        "",
        "## Declared write set (the ONLY paths you may modify)",
        _write_set_block(task),
        "",
        _ROLE_RULES[stage],
        "",
        STOP_CONTRACT,
        "",
        "# Bounded context",
        "",
        manifest.render_prompt_context(),
    ]
    if plan:
        sections += ["", "# Approved plan (follow literally)", "", plan]
    if change_summary:
        sections += ["", "# Change under review", "", change_summary]
    return "\n".join(sections)


def commit_message(
    task: Task,
    implemented_by: str | None,
    reviewed_by: str | None,
    plan_sha: str | None,
    diff_sha: str | None,
    human_approved: bool,
) -> str:
    """Commit provenance. Records only what actually happened.

    ``Reviewed-By: none`` when no independent review ran. The orchestrator does
    not name a reviewer that did not review.
    """
    title = task.path.stem.split("-", 1)[-1].replace("-", " ")
    trailers = [
        f"Task-Id: {task.id}",
        f"Requirement-Ids: {', '.join(task.implements) or 'none'}",
        f"Implemented-By: {implemented_by or 'none'}",
        f"Reviewed-By: {reviewed_by or 'none'}",
        f"Plan-Artifact-SHA256: {plan_sha or 'none'}",
        f"Diff-Artifact-SHA256: {diff_sha or 'none'}",
        f"Human-Approved: {'true' if human_approved else 'false'}",
    ]
    return f"{task.id}: {title}\n\n" + "\n".join(trailers) + "\n"
