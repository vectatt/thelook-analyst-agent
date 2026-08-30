"""State that has to outlive one turn: the rows a report is written from."""

import pytest

from analyst.agent import toolset
from analyst.tools import data as data_tool
from analyst.tools import reporting as report_tool


class NoMemories:
    def render(self, _user):
        return ""


class Index:
    trios: dict = {}


@pytest.fixture
def state():
    return {"pending_report": None, "awaiting_decision": False, "last_data": "", "last_question": ""}


def _tools(state, log):
    """One turn's tool list, over the session state dict."""
    return {t.name: t for t in toolset.build(
        bq=None, index=Index(), library=None, learned=NoMemories(), tracer=None,
        trace_id="t", user_id="u", session_id="s", log_=log, state=state,
    )}


def test_report_can_be_written_a_turn_after_the_query(state, monkeypatch):
    """"Show me revenue by brand" ... "now write that up" — two turns, one report.

    The rows live in session state rather than on the turn log: a manager asking for the numbers and
    then asking for the write-up is the normal shape of the conversation, and the second turn has a
    fresh turn log.
    """
    monkeypatch.setattr(data_tool, "fetch", lambda q, **kw: data_tool.DataResult(
        ok=True, text="brand,revenue\nCalvin Klein,120", sql="SELECT 1", rows=1))
    monkeypatch.setattr(report_tool, "write", lambda q, rows, **kw: report_tool.ReportDraft(
        ok=True, text=f"REPORT OVER: {rows}"))

    # turn 1 — the manager asks for the numbers
    turn1 = toolset.TurnLog()
    _tools(state, turn1)["get_info_from_db"].entrypoint(question="revenue by brand")

    # turn 2 — a new turn log, as the agent builds every turn; only the state carries over
    turn2 = toolset.TurnLog()
    out = _tools(state, turn2)["generate_report"].entrypoint(question="write that up")

    assert "Calvin Klein,120" in out, "the report was written without the rows from the previous turn"
    assert state["pending_report"]["body"] == out
    assert state["awaiting_decision"] is True


def test_report_without_any_data_asks_for_a_query_first(state):
    out = _tools(state, toolset.TurnLog())["generate_report"].entrypoint(question="anything")
    assert "get_info_from_db" in out
    assert state["pending_report"] is None
