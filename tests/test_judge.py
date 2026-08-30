"""Judge verdict handling — pure, no network."""

import pytest

from analyst import db as dbmod
from analyst.judge import FAIL, NA, PASS, _normalise


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "settings", SimpleNamespace(db_path=path))
    dbmod.init_db(path)
    yield path


# -- verdict normalisation -------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("pass", PASS), ("PASS", PASS), ("true", PASS), ("yes", PASS), (True, PASS),
    ("fail", FAIL), ("False", FAIL), ("no", FAIL), (False, FAIL),
    ("n/a", NA), ("N/A", NA), ("not_applicable", NA), ("none", NA), (None, NA), ("", NA),
    ("something odd", NA),                       # unknown values must not silently become a pass
])
def test_normalise(raw, expected):
    assert _normalise(raw) == expected


# -- rates must exclude n/a ------------------------------------------------------------------------
def test_untested_metric_reports_null_not_100_percent():
    """'Never applied' and 'always passed' are different facts; collapsing them hides the first."""
    import json
    from analyst.judge import summary

    with dbmod.connection() as c:
        for i, metrics in enumerate([
            {"handled_rejection_well": NA, "led_with_the_answer": PASS},
            {"handled_rejection_well": NA, "led_with_the_answer": FAIL},
        ]):
            c.execute(
                "INSERT INTO judgements (session_id, user_id, judged_at, turns, metrics, verdicts, disagreed) "
                "VALUES (?,?,?,?,?,?,0)",
                (f"s{i}", "u", "2026-08-30T00:00:00", 4, json.dumps(metrics), "{}"),
            )
    s = summary()
    assert s["pass_rate_pct"]["handled_rejection_well"] is None      # never testable
    assert s["pass_rate_pct"]["led_with_the_answer"] == 50.0         # 1 of 2 applicable
    assert s["applied_in"]["handled_rejection_well"] == "0/2"
    assert "handled_rejection_well" in s["untested_metrics"]


# -- determinism -----------------------------------------------------------------------------------
def test_every_model_call_is_deterministic_by_default():
    """Temperature must be 0 everywhere, and come from the one setting.

    Sampling has to be off for the whole system rather than most of it: the same question must give
    the same SQL and the same judge verdict, or no evaluation number can be compared between runs.
    A hard-coded override at any call site defeats that, so this checks the call sites too.
    """
    import inspect

    from analyst.agent.models import fallback_models, primary_model
    from analyst.config import settings

    assert settings.temperature == 0.0
    for model in [primary_model(), *fallback_models()]:
        assert model.temperature == 0.0, f"{type(model).__name__} {model.id} is sampling"

    # and no call site may hard-code a non-zero override
    import analyst.judge, analyst.memory, analyst.tools.data, analyst.tools.reporting
    for module in (analyst.tools.data, analyst.tools.reporting, analyst.memory, analyst.judge):  # noqa: E401
        src = inspect.getsource(module)
        for line in src.splitlines():
            if "temperature=" in line and "temperature=temperature" not in line:
                value = line.split("temperature=")[1].split(",")[0].split(")")[0].strip()
                assert value in ("0", "0.0", "None"), f"{module.__name__}: {line.strip()}"
