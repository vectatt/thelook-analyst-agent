"""`get_info_from_db` — the agent asks for information, this decides how to fetch it.

Everything SQL happens in here and nowhere else: the prompt, the generation, the guard, the retries
and the error text. The agent that called it sees only rows or a plain sentence about what could not
be answered — a failed attempt never enters its context.

Two modes:
  * `use_trio` given  → replay an analyst's verified SQL byte-for-byte. No generation, no drift.
  * otherwise         → an LLM writes SQL, and on failure is asked again *with the error attached*
                        so it does not repeat the mistake.

The rows are masked for PII before they are returned, so nothing sensitive reaches the agent's
context even if it reached the result set through a free-text column.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from analyst.bq.tool import QueryError, QueryResult, SafeBigQuery, SqlGuardError
from analyst.config import settings
from analyst.golden.models import Trio
from analyst.llm import Completion, LLMUnavailable, complete
from analyst.safety.masking import mask_dataframe
from analyst.prompts import compose
from analyst.schema import schema_markdown

log = logging.getLogger(__name__)

def _system() -> tuple[str, dict[str, str]]:
    """Composed fresh each call: an edit to sql.md or conventions.md applies to the next question."""
    rules, _, versions = compose("sql", "conventions")
    return (
        "You write one BigQuery Standard SQL query answering an analyst's question. Reply with SQL "
        "only — no prose, no markdown fence, no explanation.\n\n"
        "## Tables (use these names exactly, no dataset prefix — the system adds it)\n"
        f"{schema_markdown()}\n\n{rules}"
    ), versions


@dataclass
class DataResult:
    ok: bool
    text: str                                   # what the agent sees
    sql: str = ""
    rows: int = 0
    bytes_scanned: int = 0
    attempts: int = 1
    errors: list[str] = field(default_factory=list)
    verified: bool = False                      # produced by replaying an analyst's SQL unchanged
    masked_columns: list[str] = field(default_factory=list)
    completions: list[Completion] = field(default_factory=list)


def _render(res: QueryResult, masked: list[str], verified: bool) -> str:
    head = f"{res.rows} row(s), {res.bytes_scanned_mb:.1f} MB scanned"
    if masked:
        head += f" — columns {masked} were masked for privacy"
    body = f"{head}:\n{res.preview()}"
    if verified:
        # Say plainly that the replay is finished. Without this the agent treats a successful replay
        # as a starting point and keeps querying, which wastes time and costs the answer its verified
        # status.
        body = (
            "REPLAY OF AN ANALYST-VERIFIED QUERY — this is the analysis a human analyst wrote for exactly "
            "this question, including the decomposition needed to explain it. It is complete. Answer the "
            "user from these rows now; do not run further queries unless they ask a genuinely new "
            "question.\n\n" + body
        )
    return body


def fetch(
    question: str,
    *,
    bq: SafeBigQuery,
    trio: Trio | None = None,
    context: str = "",
    tracer=None,
    trace_id: str = "",
) -> DataResult:
    """Answer `question` with data. Replays `trio` when given, otherwise generates SQL."""
    out = DataResult(ok=False, text="")

    if trio is not None:
        sql = trio.sql
        try:
            res = _run(bq, sql, tracer, trace_id, verified=True)
        except (SqlGuardError, QueryError) as e:
            log.warning("verified trio %s failed (%s); regenerating", trio.id, e)
            out.errors.append(f"verified query failed: {e}")
        else:
            if res.rows:
                masked, cols = mask_dataframe(res.df)
                res.df = masked
                out.ok, out.sql, out.rows = True, res.sql, res.rows
                out.bytes_scanned, out.verified, out.masked_columns = res.bytes_processed, True, cols
                out.text = _render(res, cols, verified=True)
                return out
            out.errors.append("verified query returned 0 rows for this window")

    # generate
    sql = ""
    history_note = ""
    for attempt in range(1, settings.max_sql_attempts + 1):
        out.attempts = attempt
        user = _prompt(question, context, history_note, trio)
        try:
            system, prompt_versions = _system()
            completion = complete(system=system, user=user)
        except LLMUnavailable as e:
            out.text = f"I could not reach the model that writes SQL ({e}). Please try again shortly."
            return out
        out.completions.append(completion)
        sql = _strip(completion.text)
        try:
            res = _run(bq, sql, tracer, trace_id, verified=False)
        except (SqlGuardError, QueryError) as e:
            out.errors.append(str(e)[:300])
            history_note = (
                f"\n\nYour previous attempt:\n{sql}\n\nIt failed with:\n{str(e)[:400]}\n"
                f"Fix exactly that problem. Do not repeat the same mistake."
            )
            continue
        if res.rows == 0 and attempt < settings.max_sql_attempts:
            out.errors.append("0 rows")
            history_note = (
                f"\n\nYour previous attempt:\n{sql}\n\nIt ran but returned 0 rows. Usual causes: the date "
                f"window falls outside the data, a status value is spelled differently (Complete, Shipped, "
                f"Processing, Cancelled, Returned), or a brand/state/category name does not exist exactly as "
                f"written. Widen the filter or use LIKE, keeping the rest of the query."
            )
            continue
        masked, cols = mask_dataframe(res.df)
        res.df = masked
        out.ok, out.sql, out.rows = True, res.sql, res.rows
        out.bytes_scanned, out.masked_columns = res.bytes_processed, cols
        out.text = _render(res, cols, verified=False)
        return out

    out.sql = sql
    out.text = (
        f"I could not get this data after {out.attempts} attempt(s). Last problem: {out.errors[-1] if out.errors else 'unknown'}. "
        f"Tell the user plainly what could not be established, and suggest narrowing the question "
        f"(a shorter period, one region, one brand)."
    )
    return out


def _prompt(question: str, context: str, history_note: str, trio: Trio | None) -> str:
    parts = [f"Question: {question}"]
    if trio is not None:
        parts.append(f"An analyst answered a similar question with this SQL — adapt it rather than starting over:\n{trio.sql.strip()}")
    if context:
        parts.append(context)
    parts.append(f"Today is {settings.today()}.")
    return "\n\n".join(parts) + history_note


def _strip(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    return t.strip().rstrip(";")


def _run(bq: SafeBigQuery, sql: str, tracer, trace_id: str, *, verified: bool) -> QueryResult:
    if tracer is None:
        return bq.run(sql)
    with tracer.span(trace_id, "bq.run", verified=verified) as s:
        res = bq.run(sql)
        s.set(rows=res.rows, bytes=res.bytes_processed, bq_ms=res.elapsed_ms, tables=res.tables, sql=res.sql[:1500])
        return res
