"""One turn, end to end.

Thin by design: the agent decides what to do, so this layer only builds the shared objects once,
opens a trace span, and makes sure no failure ever reaches the CLI as an exception. A model outage is
reported as an outage, a guardrail as a decline, anything else as a short message with a trace id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from analyst.agent.analyst_agent import AnalystAgent, Pending
from analyst.agent.models import ModelUnavailable
from analyst.agent.toolset import TurnLog
from analyst.bq.tool import SafeBigQuery
from analyst.db import connection, init_db
from analyst.golden.store import GoldenIndex
from analyst.memory import LearnedStore
from analyst.reports.library import ReportLibrary
from analyst.tracing.jsonl import Tracer, new_trace_id

log = logging.getLogger(__name__)

_DECLINED = "[message declined by the input guard]"


@dataclass
class Turn:
    answer: str
    path: str                                   # answer | reports | rejected | error
    trace_id: str
    verified: bool = False
    sql: list[str] = field(default_factory=list)
    pending: Pending | None = None
    notes: list[str] = field(default_factory=list)
    question: str = ""
    awaiting_decision: bool = False


class Analyst:
    def __init__(self, user_id: str, session_id: str):
        init_db()
        self.user_id, self.session_id = user_id, session_id
        self.tracer = Tracer()
        self.bq = SafeBigQuery()
        self.index = GoldenIndex()
        self.library = ReportLibrary()
        self.learned = LearnedStore()
        self.agent = AnalystAgent(bq=self.bq, index=self.index, library=self.library,
                                  learned=self.learned, tracer=self.tracer,
                                  user_id=user_id, session_id=session_id)

    # -- public -----------------------------------------------------------------------------------
    def handle(self, text: str) -> Turn:
        trace_id = new_trace_id()
        text = text.strip()
        with self.tracer.span(trace_id, "turn", user_id=self.user_id, session_id=self.session_id,
                              text=text[:300]) as root:
            try:
                turn = self._handle(trace_id, text)
            except ModelUnavailable as e:
                turn = Turn(answer="The language model is unavailable right now (rate limit or outage), so I "
                                   f"could not process that. Please try again in a minute. Trace {trace_id}.",
                            path="error", trace_id=trace_id)
                root.error = f"ModelUnavailable: {str(e)[:300]}"
            except Exception as e:  # noqa: BLE001 - the CLI must survive anything
                log.exception("turn failed")
                turn = Turn(answer=f"Something went wrong on my side ({type(e).__name__}). Please try again "
                                   f"or rephrase. Trace {trace_id}.", path="error", trace_id=trace_id)
                root.error = f"{type(e).__name__}: {str(e)[:300]}"
            root.set(path=turn.path, verified=turn.verified, answer_chars=len(turn.answer))
            if turn.path == "rejected":
                root.set(text=_DECLINED)
        return turn

    def confirm(self, pending: Pending, approve: bool) -> Turn:
        trace_id = new_trace_id()
        with self.tracer.span(trace_id, "turn", user_id=self.user_id, session_id=self.session_id,
                              text=f"confirm:{approve}", path="reports") as root:
            try:
                reply = self.agent.confirm(pending, approve, trace_id)
                root.set(confirmed=approve, report_ids=pending.report_ids)
            except ModelUnavailable as e:
                root.error = f"ModelUnavailable: {str(e)[:300]}"
                return Turn(answer="The model is unavailable; the deletion was NOT carried out. Ask again in a "
                                   f"minute. Trace {trace_id}.", path="error", trace_id=trace_id)
            except Exception as e:  # noqa: BLE001
                log.exception("confirm failed")
                root.error = f"{type(e).__name__}: {str(e)[:300]}"
                return Turn(answer=f"Something went wrong finishing that ({type(e).__name__}); nothing further "
                                   f"was deleted. Check /reports. Trace {trace_id}.", path="error", trace_id=trace_id)
        answer = "Deletion cancelled — nothing was changed." if not approve else reply.answer
        return Turn(answer=answer, path="reports", trace_id=trace_id, pending=reply.pending)

    # -- the turn ---------------------------------------------------------------------------------
    def _handle(self, trace_id: str, text: str) -> Turn:
        if not text:
            return Turn(answer="Ask me about sales, customers, products or regions.", path="answer", trace_id=trace_id)
        if len(text) > 2000:
            return Turn(answer="That is too long for one question — please ask it in a sentence or two.",
                        path="rejected", trace_id=trace_id)

        reply = self.agent.run(text, trace_id)
        notes: list[str] = []

        if reply.declined:
            self._remember(_DECLINED, reply.answer, trace_id)
            return Turn(answer=reply.answer, path="rejected", trace_id=trace_id)
        if reply.pending:
            return Turn(answer=reply.answer, path="reports", trace_id=trace_id, pending=reply.pending)

        lg = reply.log
        if lg.sql_errors:
            notes.append(f"{len(lg.sql_errors)} query attempt(s) were corrected before this answer.")
        if lg.used_verified and not lg.verified_only:
            notes.append("Started from a verified analysis but ran further queries, so this is not marked verified.")
        if len(lg.sql) > 3:
            notes.append(f"{len(lg.sql)} queries were run for this answer.")
        if lg.masked_columns:
            notes.append(f"Personal data was masked in: {', '.join(sorted(set(lg.masked_columns)))}.")
        if lg.report_warnings:
            notes.append("Quality check: " + "; ".join(lg.report_warnings))
        if lg.learned:
            notes.append("Remembered: " + "; ".join(lg.learned))
        if lg.saved_report_id:
            notes.append(f"Saved as {lg.saved_report_id} and queued for analyst review.")

        self._remember(text, reply.answer, trace_id, lg)
        return Turn(answer=reply.answer, path="answer", trace_id=trace_id, verified=lg.verified_only,
                    sql=lg.sql, notes=notes, question=text,
                    awaiting_decision=bool(self.agent.state.get("awaiting_decision")))

    # -- conversation log (for the judge and /trace) -----------------------------------------------
    def _remember(self, user_text: str, answer: str, trace_id: str, lg: TurnLog | None = None) -> None:
        sql = "\n---\n".join(lg.sql) if lg and lg.sql else None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connection() as c:
            c.executemany(
                "INSERT INTO turns (session_id, user_id, ts, role, content, trace_id, sql) VALUES (?,?,?,?,?,?,?)",
                [(self.session_id, self.user_id, now, "user", user_text, trace_id, None),
                 (self.session_id, self.user_id, now, "assistant", answer, trace_id, sql)],
            )
