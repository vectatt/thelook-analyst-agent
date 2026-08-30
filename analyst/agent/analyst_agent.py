"""The agent: one Agno agent, a bounded tool loop, and a pause before anything is destroyed.

Deciding whether a question needs the verified-analysis library is the agent's own job: `check_goldens`
is a tool it calls, because only it can tell whether "I like it" is a data question or a verdict on the
last report.

State that must outlive a single run — the rows the last query returned, the report awaiting the
manager's verdict — lives in Agno's `session_state`, which is stored alongside the session, so it
survives both the next turn and a restart of the process.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.guardrails import PIIDetectionGuardrail, PromptInjectionGuardrail

from analyst.agent.models import ModelUnavailable, fallback_models, guardrail_fired, primary_model, run_failed
from analyst.agent.toolset import TurnLog, build
from analyst.bq.tool import SafeBigQuery
from analyst.config import settings
from analyst.golden.store import GoldenIndex
from analyst.memory import LearnedStore
from analyst.prompts import compose
from analyst.reports.library import ReportLibrary
from analyst.tracing.jsonl import Tracer


@dataclass
class Pending:
    """A destructive tool call waiting for the manager's typed confirmation."""
    run_id: str
    session_id: str
    report_ids: list[str]
    titles: dict[str, str] = field(default_factory=dict)


@dataclass
class Reply:
    answer: str
    pending: Pending | None = None
    log: TurnLog = field(default_factory=TurnLog)
    declined: bool = False


class AnalystAgent:
    def __init__(self, *, bq: SafeBigQuery, index: GoldenIndex, library: ReportLibrary,
                 learned: LearnedStore, tracer: Tracer, user_id: str, session_id: str):
        self.bq, self.index, self.library, self.learned, self.tracer = bq, index, library, learned, tracer
        self.user_id, self.session_id = user_id, session_id
        self.db = SqliteDb(db_file=str(settings.agno_db_path))
        self.state: dict = {"pending_report": None, "awaiting_decision": False,
                            "last_data": ""}

    # -- construction -----------------------------------------------------------------------------
    def _instructions(self) -> tuple[str, dict[str, str]]:
        """Composed on every turn: an edit to any prompt layer applies to the next message."""
        text, _, versions = compose("agent", "persona", "conventions")
        known = self.learned.render(self.user_id)
        if known:
            text += (
                "\n\n## What you have learned about this manager\n"
                "These are your own observations. Follow them unless they conflict with the instructions "
                "above, which always win.\n" + known
            )
        if self.state.get("awaiting_decision"):
            text += (
                "\n\n## A report is awaiting their verdict\n"
                "You have just shown a report. If their next message approves it (\"yes\", \"good\", \"save it\"), "
                "call save_report. If they reject it, ask what was wrong unless they already said; once you know, "
                "call generate_report with feedback=\"...\" — and if the problem was the DATA, call "
                "get_info_from_db again first. If they change the subject instead, drop it and answer the new "
                "question."
            )
        return text, versions

    def _build(self, trace_id: str, log: TurnLog) -> tuple[Agent, dict[str, str]]:
        instructions, versions = self._instructions()
        tools = build(bq=self.bq, index=self.index, library=self.library, learned=self.learned,
                      tracer=self.tracer, trace_id=trace_id, user_id=self.user_id,
                      session_id=self.session_id, log_=log, state=self.state)
        agent = Agent(
            name="analyst",
            model=primary_model(),
            fallback_models=fallback_models(),
            tools=tools,
            instructions=instructions,
            pre_hooks=[PromptInjectionGuardrail(), PIIDetectionGuardrail()],
            db=self.db,
            # The same dict object every turn, so the tools mutate what the next turn reads; Agno
            # writes it back to SQLite with the session.
            session_state=self.state,
            add_history_to_context=True,
            num_history_runs=settings.history_runs,
            tool_call_limit=settings.tool_call_limit,
            retries=2,
            exponential_backoff=True,
            markdown=True,
        )
        return agent, versions

    # -- running ----------------------------------------------------------------------------------
    def run(self, text: str, trace_id: str) -> Reply:
        log = TurnLog()
        agent, versions = self._build(trace_id, log)
        with self.tracer.span(trace_id, "llm.agent", prompts=versions) as s:
            run = agent.run(text, user_id=self.user_id, session_id=self.session_id)
            s.set(checked_goldens=log.checked_goldens, sql_attempts=log.sql_attempts,
                  verified=log.verified_only, queries=len(log.sql), trios=log.trios_seen, **_usage(run))
        return self._outcome(run, log)

    def confirm(self, pending: Pending, approve: bool, trace_id: str) -> Reply:
        """Resume a paused run. Reloaded from SQLite, so this works from a fresh process too."""
        log = TurnLog()
        agent, _ = self._build(trace_id, log)
        run = agent.get_run_output(run_id=pending.run_id, session_id=pending.session_id)
        if run is None or not run.is_paused:
            return Reply(answer="That deletion is no longer pending.")
        for req in run.active_requirements:
            if req.needs_confirmation:
                req.confirm() if approve else req.reject(
                    "The user cancelled. Nothing was deleted and the confirmation prompt is gone. "
                    "Tell them nothing was changed. If they ask to delete again, call delete_reports "
                    "again — that is what raises a new prompt; never claim one is already showing."
                )
        with self.tracer.span(trace_id, "llm.agent.continue", confirmed=approve) as s:
            run = agent.continue_run(run_id=pending.run_id, requirements=run.requirements,
                                     session_id=pending.session_id, user_id=self.user_id)
            s.set(**_usage(run))
        return self._outcome(run, log)

    # -- interpreting the run ---------------------------------------------------------------------
    def _outcome(self, run, log: TurnLog) -> Reply:
        if guardrail_fired(run):
            return Reply(answer="I can only help with analysis of our sales, customer and product data. "
                                "(That input was declined by the safety check.)", log=log, declined=True)
        failure = run_failed(run)
        if failure:
            raise ModelUnavailable(failure)
        if run.is_paused:
            ids: list[str] = []
            for req in run.active_requirements:
                if req.needs_confirmation and req.tool_execution.tool_name == "delete_reports":
                    ids += list(req.tool_execution.tool_args.get("report_ids", []))
            titles = {}
            for rid in ids:
                r = self.library.get(self.user_id, rid)
                titles[rid] = f"{r.title} — {r.description or r.question[:60]}" if r else "(not found / not yours)"
            return Reply(answer="", pending=Pending(run.run_id, run.session_id, ids, titles), log=log)
        answer = str(run.content or "").strip()
        if not answer:
            raise ModelUnavailable("the agent returned an empty answer")
        return Reply(answer=answer, log=log)


def _usage(run) -> dict:
    m = getattr(run, "metrics", None)
    if not m:
        return {}
    return {"input_tokens": getattr(m, "input_tokens", None), "output_tokens": getattr(m, "output_tokens", None)}
