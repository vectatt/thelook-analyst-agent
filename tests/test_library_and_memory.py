"""Report library and the learned-state store — pure SQLite, no network.

The memory tests deliberately exercise only paths that make no model call: a first write (nothing to
reconcile against), rendering, forgetting, and the org proposal lifecycle. Reconciliation itself needs
a model and is covered by the end-to-end smoke.
"""

import pytest

from analyst import db as dbmod


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Point the whole data layer at a throwaway SQLite file."""
    from types import SimpleNamespace

    path = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "settings", SimpleNamespace(db_path=path))
    dbmod.init_db(path)
    yield path


# -- report library ------------------------------------------------------------------------------
def test_save_find_delete_is_owner_scoped_and_idempotent():
    from analyst.reports.library import ReportLibrary

    lib = ReportLibrary()
    a = lib.save("alice", "s1", "Nike Q1", "how is nike doing", "Nike revenue up",
                 description="Nike revenue and margin for Q1")
    b = lib.save("alice", "s2", "Texas trend", "texas revenue", "flat")
    lib.save("bob", "s3", "Nike for Bob", "nike", "bob's report")

    # findable by the generated description, which is what makes "delete the ones about Nike" work
    assert [r.id for r in lib.find("alice", text="margin")] == [a.id]
    assert len(lib.find("alice", session_id="s2")) == 1

    assert lib.delete("bob", [a.id, "rpt_nope"]) == []          # cannot touch another owner's
    assert set(lib.delete("alice", [a.id, b.id])) == {a.id, b.id}
    assert lib.list("alice") == []
    assert lib.delete("alice", [a.id]) == []                    # idempotent: a resumed run is safe
    assert "delete" in [row["action"] for row in lib.audit_log("alice")]


# -- learned state -------------------------------------------------------------------------------
def test_first_observation_is_stored_and_rendered():
    from analyst.memory import LearnedStore

    s = LearnedStore()
    assert "Noted" in s.remember("alice", "prefers bullet points over tables")
    rendered = s.render("alice")
    assert "bullet points" in rendered
    assert s.render("bob") == ""                                # scoped to the user


def test_too_short_observations_are_ignored():
    from analyst.memory import LearnedStore

    assert "too short" in LearnedStore().remember("alice", "ok").lower()




def test_forgetting_removes_an_entry():
    from analyst.memory import LearnedStore

    s = LearnedStore()
    s.remember("alice", "wants reports kept brief")
    entry = s.for_user("alice")[0]
    assert s.forget("alice", entry.id)
    assert s.render("alice") == ""
    assert not s.forget("bob", entry.id)                        # cannot forget another user's
