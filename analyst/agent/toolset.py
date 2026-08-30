"""The agent's tools, bound to one user, one session and one turn.

Everything the agent can do is here. Three of these tools make their own model call internally
(`get_info_from_db`, `generate_report`, and the reconcile inside `remember`) — that is deliberate:
drafts, failed SQL and error text stay inside the tool and never enter the agent's context.

Scope is fixed in the closure. The model passes report ids and text; it never passes a user id, so it
cannot reach another manager's library however it is prompted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agno.tools import tool

from analyst.bq.tool import SafeBigQuery
from analyst.golden import candidates
from analyst.golden.store import GoldenIndex
from analyst.memory import LearnedStore
from analyst.reports.library import ReportLibrary
from analyst.tools import data as data_tool
from analyst.tools import goldens as goldens_tool
from analyst.tools import reporting as report_tool
from analyst.tools import schema_tool

log = logging.getLogger(__name__)


@dataclass
class TurnLog:
    """What the tools did this turn — the session layer reads it; the judge reads it from the trace."""
    sql: list[str] = field(default_factory=list)
    sql_attempts: int = 0
    sql_errors: list[str] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    checked_goldens: bool = False
    trios_seen: list[str] = field(default_factory=list)
    used_verified: bool = False        # a verified replay ran at some point this turn
    masked_columns: list[str] = field(default_factory=list)
    report_warnings: list[str] = field(default_factory=list)
    saved_report_id: str | None = None
    learned: list[str] = field(default_factory=list)

    @property
    def verified_only(self) -> bool:
        """True only when the answer rests on the analyst's query and nothing else.

        The badge means "a human wrote and checked the query behind this". If a replay is followed by
        further ad-hoc queries the answer is a mixture, and does not qualify.
        """
        return self.used_verified and len(self.sql) == 1


def build(
    *,
    bq: SafeBigQuery,
    index: GoldenIndex,
    library: ReportLibrary,
    learned: LearnedStore,
    tracer,
    trace_id: str,
    user_id: str,
    session_id: str,
    log_: TurnLog,
    state: dict,
) -> list:
    """Construct the tool list for one turn. `state` is the agent's session_state dict."""

    # -- knowing what exists ----------------------------------------------------------------------
    @tool
    def get_schema(detail: bool = False) -> str:
        """Describe the data available: the tables, what each contains, how the key metrics are defined
        here, and which questions already have an analyst-verified answer. Call this when the user asks
        what data exists or what they can ask, and before writing SQL for an unfamiliar area.
        Set detail=True to also list every column."""
        return schema_tool.describe(index=index, detail=detail, tracer=tracer, trace_id=trace_id)

    @tool
    def check_goldens(question: str) -> str:
        """Search analyses that human analysts already wrote and verified for this dataset. ALWAYS call
        this before answering a data question: the analyst notes carry business rules (which order
        statuses count as revenue, how to compare regions fairly) that are not in the schema and that you
        cannot infer. If a match is strong you will be told its id, which you can replay exactly."""
        found = goldens_tool.look_up(question, index=index, tracer=tracer, trace_id=trace_id)
        log_.checked_goldens = True
        log_.trios_seen.extend(m.trio.id for m in found.matches)
        return found.text

    # -- getting data -----------------------------------------------------------------------------
    @tool
    def get_info_from_db(question: str = "", use_trio: str = "") -> str:
        """Get information from the warehouse. Describe what you need in plain words — this writes and
        runs the SQL for you, corrects its own errors, and returns the rows with personal data masked.
        Pass use_trio="<id>" to replay an analyst-verified query exactly (from check_goldens); leave it
        empty for a fresh query. Call it again for a follow-up figure — chaining two calls is normal for
        a multi-step question."""
        trio = index.trios.get(use_trio) if use_trio else None
        if use_trio and trio is None:
            return f"There is no verified analysis with id '{use_trio}'. Call check_goldens first, or leave use_trio empty."
        # Called with only `use_trio` this should still work: the trio's own question is the sensible
        # default, and a validation error here surfaces to the user as the agent apologising.
        question = question or (trio.question if trio else "")
        if not question:
            return "Tell me what information you need — pass `question`, or a `use_trio` id from check_goldens."
        result = data_tool.fetch(question, bq=bq, trio=trio, tracer=tracer, trace_id=trace_id)
        log_.sql.extend([result.sql] if result.sql else [])
        log_.sql_attempts += result.attempts
        log_.sql_errors.extend(result.errors)
        log_.masked_columns.extend(result.masked_columns)
        if result.ok:
            log_.rows.append(result.rows)
            log_.used_verified = log_.used_verified or result.verified
            # Kept in session state, not on the turn log: managers routinely ask for the numbers in one
            # message and the report in the next, and the rows have to still be there when they do.
            state["last_data"] = result.text
        return result.text

    # -- writing the answer -----------------------------------------------------------------------
    @tool
    def generate_report(question: str, feedback: str = "") -> str:
        """Turn the data you just fetched into the manager's report — headline, findings, why, and action
        items — in their preferred style, using what is known about them. Call this after
        get_info_from_db for anything the user asked to be a report, or when revising one: pass
        feedback="<what they said was wrong>" to rewrite it addressing that specifically. If they said
        the DATA was wrong, call get_info_from_db again first, then call this."""
        rows = state.get("last_data")
        if not rows:
            return "There is no data to write about yet — call get_info_from_db first."
        draft = report_tool.write(
            question,
            rows,
            memories=learned.render(user_id),
            feedback=feedback,
            tracer=tracer,
            trace_id=trace_id,
        )
        log_.report_warnings = draft.warnings
        state["pending_report"] = {"question": question, "body": draft.text}
        state["awaiting_decision"] = True
        if not draft.ok and draft.warnings:
            return (draft.text + "\n\n[quality check flagged: " + "; ".join(draft.warnings)
                    + " — tell the user this could not be fully verified]")
        return draft.text

    # -- the report library -----------------------------------------------------------------------
    def _fmt(reports) -> str:
        if not reports:
            return "No matching reports."
        return "\n".join(f"[{r.id}] {r.title} — {r.description or r.question[:60]} ({r.created_date})"
                         for r in reports)

    @tool
    def save_report(title: str = "") -> str:
        """Save the report you just produced to this manager's library, once they have approved it.
        A one-line description is generated so it can be found later by topic, and the analysis is queued
        for an analyst to consider adding to the verified library."""
        pending = state.get("pending_report")
        if not pending:
            return "There is no report waiting to be saved — generate one first."
        description = report_tool.describe(pending["question"], pending["body"], tracer=tracer, trace_id=trace_id)
        saved = library.save(user_id, session_id, (title or pending["question"])[:120],
                             pending["question"], pending["body"], description=description)
        log_.saved_report_id = saved.id
        candidates.queue(owner=user_id, session_id=session_id, question=pending["question"],
                         sql="\n---\n".join(log_.sql), report=pending["body"])
        state["pending_report"] = None
        state["awaiting_decision"] = False
        return f"Saved as [{saved.id}] {saved.title}. It is also queued for review as a verified analysis."

    @tool
    def get_reports(about: str = "") -> str:
        """List this manager's saved reports with their descriptions. Pass `about` to narrow by topic,
        client, brand or region — do this before deleting anything, so you delete the right ones."""
        return _fmt(library.find(user_id, text=about) if about else library.list(user_id))

    @tool
    def get_reports_from_this_conversation() -> str:
        """List the reports saved during the current conversation. Use for "the reports we made here"."""
        return _fmt(library.find(user_id, session_id=session_id))

    @tool
    def show_report(report_id: str) -> str:
        """Show the full text of one saved report."""
        r = library.get(user_id, report_id)
        return f"# {r.title}\n\n{r.body}" if r and not r.deleted_at else "No such report."

    @tool(requires_confirmation=True)
    def delete_reports(report_ids: list[str]) -> str:
        """Start deleting saved reports. CALLING THIS DELETES NOTHING BY ITSELF — it pauses and shows the
        user exactly which reports are involved, and they must type DELETE before anything happens. That
        makes it the safe way to handle a deletion request.

        So: find the ids with get_reports, then call this immediately. Do NOT ask the user "shall I
        delete this?" or tell them to type DELETE yourself — this call is how that prompt appears. If
        nothing matched their request, say so and do not call this at all."""
        deleted = library.delete(user_id, report_ids)
        skipped = sorted(set(report_ids) - set(deleted))
        msg = f"Deleted {len(deleted)} report(s): {', '.join(deleted) or '-'}."
        if skipped:
            msg += f" Skipped (not yours or already deleted): {', '.join(skipped)}."
        return msg

    # -- learning ---------------------------------------------------------------------------------
    @tool
    def remember(observation: str) -> str:
        """Record something lasting about how this manager wants to be answered — "prefers bullets",
        "always wants regions compared per customer", "does not want charts". Call it when they state a
        preference or reject a report for a reason that will apply again, not for a one-off request."""
        result = learned.remember(user_id, observation, tracer=tracer, trace_id=trace_id)
        log_.learned.append(observation)
        return result

    @tool
    def what_i_know_about_you() -> str:
        """Show what has been learned about this manager's preferences, so they can see why answers look
        the way they do."""
        return learned.render(user_id) or "Nothing learned yet — tell me how you like your reports."

    return [get_schema, check_goldens, get_info_from_db, generate_report,
            save_report, get_reports, get_reports_from_this_conversation, show_report,
            delete_reports, remember, what_i_know_about_you]
