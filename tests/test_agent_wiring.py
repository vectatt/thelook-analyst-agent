"""The agent assembles offline.

No network: this only builds the Agent object and its tool list. It exists because every other test
exercises modules in isolation, so a signature change in the model layer or the toolset reaches the
agent only at runtime — where it costs a live BigQuery run to discover.
"""

import pytest

from analyst.agent.analyst_agent import AnalystAgent
from analyst.tracing.jsonl import Tracer


class NoMemories:
    def render(self, _user):
        return ""


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """Settings are frozen, so point the Agno session db at a temp file via the property it reads."""
    from analyst.config import Settings
    monkeypatch.setattr(Settings, "agno_db_path", property(lambda self: tmp_path / "agno.db"))
    return AnalystAgent(bq=None, index=None, library=None, learned=NoMemories(),
                        tracer=Tracer(tmp_path), user_id="u", session_id="s")


def test_agent_builds_with_its_tools_and_model(agent):
    from analyst.agent.toolset import TurnLog

    built, versions = agent._build("trace", TurnLog())
    assert built.model is not None
    assert built.session_state is agent.state          # tools mutate what the next turn reads
    assert versions, "prompt layers should be versioned for trace attribution"

    names = {t.name for t in built.tools}
    assert {"check_goldens", "get_info_from_db", "generate_report", "delete_reports"} <= names
    assert "undo_delete" not in names


def test_delete_is_the_only_tool_that_pauses(agent):
    from analyst.agent.toolset import TurnLog

    built, _ = agent._build("trace", TurnLog())
    pausing = {t.name for t in built.tools if getattr(t, "requires_confirmation", False)}
    assert pausing == {"delete_reports"}
