"""`generate_report` — turn queried data into the manager's report, with a validating post-hook.

An LLM call inside a tool, so drafts and retries never enter the agent's context. What goes in: the
question, the rows that came back, what this manager is known to like, and — on a revision — exactly
what they said was wrong.

The post-hook is where quality is enforced rather than hoped for. Three checks, in order of how often
they catch something:

  1. **grounded** — every figure in the report appears in the data it was given. This is the check
     that matters: a report that invents a number is worse than no report.
  2. **structured** — a report has a headline, findings and action items; a wall of prose does not.
  3. **non-empty**

A failed check is not a failure of the turn: the draft goes back to the model with the specific
problem named, once. A second failure returns the draft with the warning attached, so the agent can
tell the user rather than silently presenting something unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from analyst.config import settings
from analyst.llm import Completion, LLMUnavailable, complete
from analyst.prompts import compose
from analyst.tracing.jsonl import optional_span

def _system() -> tuple[str, dict[str, object], dict[str, str]]:
    """Composed fresh each call, so a CEO editing persona.md changes the next report — no redeploy.

    Returns the prompt, the policy the post-hook must enforce, and the layer versions for the trace.
    """
    text, policy, versions = compose("persona", "report", "conventions")
    return (
        "You are an in-house retail data analyst writing for a manager who is not technical.\n"
        "You are given a question, the rows that answer it, and what is known about this manager.\n\n"
        + text
    ), policy, versions

# Figures a report asserts: currency, thousands separators, decimal percentages.
_FIGURE = re.compile(r"[$€£]\s?[\d,]+(?:\.\d+)?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\s?%")
_SECTION = re.compile(r"action item", re.I)


@dataclass
class ReportDraft:
    ok: bool
    text: str
    warnings: list[str] = field(default_factory=list)
    attempts: int = 1
    completions: list[Completion] = field(default_factory=list)


def _numbers_in(text: str) -> list[tuple[str, float]]:
    """Figures the report asserts, as (what was written, its value)."""
    out = []
    for m in _FIGURE.finditer(text):
        raw = m.group(0)
        cleaned = re.sub(r"[^\d.]", "", raw).strip(".")
        if not cleaned:
            continue
        try:
            out.append((raw.strip(), float(cleaned)))
        except ValueError:
            continue
    return out


def _ungrounded_figures(report: str, data: str) -> list[str]:
    """Figures in the report with no corresponding value in the data it was given.

    Compared numerically with tolerance rather than as strings, because the persona instructs the
    writer to round money to whole dollars: a faithful report says $7,852 for a queried 7851.78. A
    figure counts as grounded if some data value matches within 1% or rounds to it; anything else is
    invented, including a total the writer summed itself — the prompts require aggregates to be
    queried, not computed, precisely so every figure stays traceable to a row.
    """
    values = [float(d) for d in re.findall(r"\d+(?:\.\d+)?", re.sub(r"[^\d.\s]", " ", data))]
    missing = []
    for raw, figure in _numbers_in(report):
        if any(
            abs(figure - v) <= max(0.01 * max(abs(figure), abs(v)), 0.51)   # 1%, or a rounding step
            or round(v) == round(figure)
            or (v and round(v, 1) == round(figure, 1))
            for v in values
        ):
            continue
        missing.append(raw)
    return missing


def validate(report: str, data: str, *, want_actions: bool, policy: dict[str, object] | None = None) -> list[str]:
    """Post-hook. Returns the problems found; empty means the draft passes.

    Grounding is checked first because it is the one that matters: a report that invents a number is
    worse than no report. The rest comes from the persona file's policy block, so a non-developer can
    tighten "keep it under 300 words" into something enforced rather than merely requested.
    """
    policy = policy or {}
    problems: list[str] = []
    if not report.strip():
        return ["the report is empty"]

    missing = _ungrounded_figures(report, data)
    if missing:
        problems.append(
            f"these figures do not appear in the data you were given: {sorted(missing)[:6]} — "
            f"every number must come from the rows provided"
        )

    required = policy.get("require_sections") or ([] if not want_actions else ["action items"])
    if isinstance(required, str):
        required = [required]
    for section in required:
        if section.lower() not in report.lower():
            problems.append(f"the required '{section}' section is missing")

    max_words = policy.get("max_words")
    if isinstance(max_words, int):
        words = len(report.split())
        if words > max_words:
            problems.append(f"the report is {words} words, over the {max_words}-word limit set in the persona")

    if len(report.strip()) < 120:
        problems.append("the report is too short to be useful")
    return problems


def write(
    question: str,
    data: str,
    *,
    preferences: str = "",
    memories: str = "",
    feedback: str = "",
    want_actions: bool = True,
    tracer=None,
    trace_id: str = "",
) -> ReportDraft:
    """Draft a report and validate it, retrying once with the problem named."""
    draft = ReportDraft(ok=False, text="")
    note = ""

    for attempt in range(1, settings.max_report_attempts + 1):
        draft.attempts = attempt
        system, policy, prompt_versions = _system()
        user = _prompt(question, data, preferences, memories, feedback, note)
        with optional_span(tracer, trace_id, "llm.report", attempt=attempt) as span:
            try:
                completion = complete(system=system, user=user)
            except LLMUnavailable as e:
                draft.text = f"I could not reach the model that writes reports ({e}). Try again shortly."
                return draft
            span.set(**completion.usage())

        draft.completions.append(completion)
        problems = validate(completion.text, data, want_actions=want_actions, policy=policy)
        if not problems:
            draft.ok, draft.text = True, completion.text
            return draft
        draft.warnings = problems
        note = (
            f"\n\nYour previous draft was rejected by the quality check:\n- " + "\n- ".join(problems) +
            "\nRewrite it fixing exactly those problems. Use only the figures present in the data above."
        )

    # second failure: hand back the draft, flagged, rather than nothing
    draft.text = draft.completions[-1].text if draft.completions else ""
    draft.ok = False
    return draft


def _prompt(question: str, data: str, preferences: str, memories: str, feedback: str, note: str) -> str:
    parts = [f"Question: {question}"]
    if preferences:
        parts.append(f"How this manager likes reports: {preferences}")
    if memories:
        parts.append(f"What we know about this manager:\n{memories}")
    if feedback:
        parts.append(
            f"IMPORTANT — this is a revision. The manager rejected the previous report and said:\n"
            f"\"{feedback}\"\nAddress that specifically; do not simply reword the same report."
        )
    parts.append(f"Data (this is the only source of numbers you may use):\n{data}")
    parts.append(f"Today is {settings.today()}.")
    return "\n\n".join(parts) + note


def describe(question: str, report: str, *, tracer=None, trace_id: str = "") -> str:
    """One-line description of a saved report, so it can be found later by topic."""
    with optional_span(tracer, trace_id, "llm.describe"):
        try:
            out = complete(
            system="Summarise what this analysis is about in one sentence, under 25 words. Name the metric, "
                   "the dimension and any filter (region, brand, category, period). No preamble.",
            user=f"Question: {question}\n\nReport:\n{report[:1500]}",
            )
            return out.text.strip().strip('"')[:200]
        except LLMUnavailable:
            return question[:200]
