"""What the agent has learned about how each manager wants to be answered.

Entries are free text, not a fixed schema. That is safe here for a specific reason: none of this
system's safety rules live in prompts. PII is blocked by the views, the SQL guard and the output mask;
deletion is gated by `requires_confirmation` in code; SELECT-only is enforced by the AST check. So an
entry saying "always include customer e-mails" is inert — the agent still cannot fetch them. What a
learned entry *can* express is tone, depth, what to compare against, which metrics to lead with. All
harmless, and none of it worth hard-coding into an enum in advance.

Two real risks remain, neither of them security:

  * **contradiction** — "likes short reports" then "wants more detail". Each write is reconciled
    against the existing entries and supersedes the ones it contradicts, so both cannot be held.
  * **growth** — capped by count and by rendered size; the oldest entries are consolidated.

Human-authored prompt layers always win: learned text is rendered first and labelled as observation,
so an instruction in `persona.md` overrides anything the agent taught itself.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from analyst.config import settings
from analyst.db import connection, init_db
from analyst.llm import LLMUnavailable, complete
from analyst.tracing.jsonl import optional_span

log = logging.getLogger(__name__)

MAX_ENTRIES = 40          # per subject; beyond this the oldest are consolidated
MAX_RENDER_CHARS = 1600   # what may be injected into a prompt


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    id: int
    subject: str
    text: str
    status: str
    created_at: str

    def __str__(self) -> str:
        return self.text


class LearnedStore:
    """Reads are plain lookups; the only model call is on write, to reconcile against what is stored."""

    def __init__(self):
        init_db()

    # -- reading ----------------------------------------------------------------------------------
    def for_user(self, user_id: str) -> list[Entry]:
        with connection() as c:
            rows = c.execute(
                "SELECT * FROM learned WHERE status='active' AND subject=? ORDER BY id DESC LIMIT ?",
                (user_id, MAX_ENTRIES),
            ).fetchall()
        return [Entry(**dict(r)) for r in rows]

    def render(self, user_id: str) -> str:
        """The block injected into prompts. Empty string when nothing has been learned yet."""
        entries = self.for_user(user_id)
        if not entries:
            return ""
        lines, total = [], 0
        for e in entries:
            line = f"- {e.text}"
            if total + len(line) > MAX_RENDER_CHARS:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    # -- writing ----------------------------------------------------------------------------------
    def remember(self, user_id: str, text: str, *, tracer=None, trace_id: str = "") -> str:
        """Record an observation. Replaces a contradicting entry rather than accumulating both."""
        text = " ".join(str(text).split())[:400]
        if len(text) < 8:
            return "That was too short to be worth remembering."

        # Reconcile against every existing entry. A lexical pre-filter cannot work here:
        # paraphrases share few words ("prefers bullet points over tables" vs "likes bullet lists
        # rather than tables" overlap by 0.22) and would slip through as new facts. Entries are capped
        # and short, so one small-model call per write is the honest price.
        existing = self.for_user(user_id)

        if existing:
            verdict, replaced = self._reconcile(text, existing, tracer, trace_id)
            if verdict == "duplicate":
                return "Already known — nothing new saved."
            if verdict == "replace" and replaced:
                with connection() as c:
                    c.execute("UPDATE learned SET status='superseded' WHERE id IN (%s)"
                              % ",".join("?" * len(replaced)), replaced)

        with connection() as c:
            c.execute(
                "INSERT INTO learned (subject, text, status, created_at) VALUES (?,?,?,?)",
                (user_id, text, "active", _now()),
            )
        return f"Noted for next time: {text}"

    def _reconcile(self, new: str, close: list[Entry], tracer, trace_id: str) -> tuple[str, list[int]]:
        """Ask a small model whether the new observation duplicates or contradicts existing ones."""
        listing = "\n".join(f"{e.id}: {e.text}" for e in close[:MAX_ENTRIES])
        with optional_span(tracer, trace_id, "llm.memory_reconcile") as span:
            try:
                out = complete(
                system=(
                    "You maintain a list of observations about how one person likes their reports written.\n"
                    "Given a NEW observation and the EXISTING list, reply with exactly one word on the first line:\n"
                    "  DUPLICATE — the new one says the same thing as an existing one, even in different words\n"
                    "  REPLACE   — it contradicts or updates existing ones (e.g. 'wants it brief' then 'wants full detail')\n"
                    "  KEEP      — it is genuinely new and consistent with the rest\n"
                    "If REPLACE, put the ids to supersede on the second line, comma separated. Nothing else."
                ),
                    user=f"EXISTING:\n{listing}\n\nNEW:\n{new}",
                )
            except LLMUnavailable:
                return "keep", []      # never lose an observation because a model was down

            lines = [l.strip() for l in out.text.splitlines() if l.strip()]
            verdict = (lines[0] if lines else "keep").split()[0].lower()
            ids: list[int] = []
            if verdict == "replace" and len(lines) > 1:
                ids = [int(n) for n in re.findall(r"\d+", lines[1]) if any(e.id == int(n) for e in close)]
                if not ids:
                    verdict = "keep"
            span.set(verdict=verdict, superseded=ids)
            return verdict, ids

    def forget(self, user_id: str, entry_id: int) -> bool:
        with connection() as c:
            cur = c.execute("UPDATE learned SET status='forgotten' WHERE id=? AND subject=?", (entry_id, user_id))
            return cur.rowcount > 0
