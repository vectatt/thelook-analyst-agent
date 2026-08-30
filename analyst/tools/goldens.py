"""`check_goldens` — retrieve what human analysts already worked out for a question like this one.

A tool rather than an automatic step, because only the agent knows when a message is analytical.
Embedding every message ("I like it", "delete that one") costs a request and matches nothing.

What comes back is the analyst's *notes* — the business rules that are not in the schema — and a
`trio_id`. Deliberately not the SQL: the agent's job is to decide which analysis applies, not to read
or edit queries. The SQL is replayed inside `get_info_from_db`, which never shows it either.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analyst.config import settings
from analyst.golden.store import GoldenIndex, Match
from analyst.tracing.jsonl import optional_span


@dataclass
class GoldenLookup:
    text: str                                   # what the agent sees
    best_trio_id: str | None = None             # safe to replay verbatim
    matches: list[Match] = field(default_factory=list)
    degraded: bool = False                      # embeddings unavailable, keyword scoring used


def look_up(question: str, *, index: GoldenIndex, tracer=None, trace_id: str = "") -> GoldenLookup:
    with optional_span(tracer, trace_id, "tool.check_goldens") as span:
        matches, degraded = index.search(question, k=3)
        if not matches:
            out = GoldenLookup(
                text="No verified analysis is close to this question. Answer it from the schema and the "
                     "standing conventions instead.",
                degraded=degraded,
            )
        else:
            best = matches[0]
            replayable = best.score >= settings.tau_replay and not degraded
            parts = [
                f"### {m.trio.question}   (similarity {m.score:.2f}, id: {m.trio.id})\n"
                f"Analyst notes: {m.trio.analyst_notes.strip()}"
                for m in matches
            ]
            guidance = (
                f"\n\nThe closest match (id `{best.trio.id}`) is a strong match. Call get_info_from_db "
                f"with use_trio=\"{best.trio.id}\" to run the analyst's verified query for it; the answer "
                f"can then be presented as verified."
                if replayable else
                "\n\nNone of these is close enough to reuse directly. Treat the analyst notes as rules and "
                "let get_info_from_db work out the query for this question."
            )
            out = GoldenLookup(
                text="\n\n".join(parts) + guidance,
                best_trio_id=best.trio.id if replayable else None,
                matches=matches,
                degraded=degraded,
            )
        if degraded:
            out.text += "\n\n(Note: the embedding service was unavailable; these were matched on keywords only.)"
        span.set(hits=[m.trio.id for m in out.matches],
                 best=round(out.matches[0].score, 3) if out.matches else 0.0,
                 replayable=out.best_trio_id, degraded=degraded)
        return out
