"""`get_schema` — answer "what data do we have, and what can I ask?"

The brief lists this as a required capability, and it needs no model and no query: the four views are
known, and the golden bucket already lists the questions that have verified answers. Cheapest possible
tool, and the one that makes the assistant discoverable to a manager who has never used it.
"""

from __future__ import annotations

from analyst.golden.store import GoldenIndex
from analyst.prompts import load
from analyst.schema import schema_markdown, schema_overview
from analyst.tracing.jsonl import optional_span


def describe(*, index: GoldenIndex | None = None, detail: bool = False, tracer=None, trace_id: str = "") -> str:
    with optional_span(tracer, trace_id, "tool.get_schema"):
        parts = [schema_overview()]
        if detail:
            parts.append("## Columns\n" + schema_markdown())
        conventions = load("conventions").text
        if conventions:
            parts.append("## How these numbers are defined here\n" + conventions)
        if index is not None and index.trios:
            examples = "\n".join(f"  · {t.question}" for t in list(index.trios.values())[:8])
            parts.append("## Questions with an analyst-verified answer ready\n" + examples)
        return "\n\n".join(parts)
