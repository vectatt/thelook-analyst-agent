"""Behavioural scenarios: one per capability the brief names, asserted rather than eyeballed.

`smoke_agent.py` walks one long conversation and prints it for a human to read. This is the other half
of pre-deployment checking: independent sessions, each making a claim about behaviour that a person
would otherwise have to notice by reading transcripts — that a follow-up runs its own query, that one
manager's preferences never reach another's, that an undefined metric is not quietly given a value.

Each scenario gets its own user and session, so nothing leaks between them. Exits non-zero on failure.
"""
from __future__ import annotations

import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyst.agent.session import Analyst

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"    {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(f"{name}: {detail}")


def run(a: Analyst, msg: str, label: str = ""):
    t = a.handle(msg)
    print(f"  > {msg[:70]}")
    print(f"    path={t.path} verified={t.verified} sql={len(t.sql)} :: {(t.answer or '(none)')[:160]}")
    return t


def s1_product_comparison():
    """Brief: 'compare performance of product X and Y, and why do they perform differently'."""
    print("\n### 1. product comparison + why")
    a = Analyst(user_id="mgr_prod", session_id="sc1-" + str(int(time.time())))
    t = run(a, "Compare the performance of our Levi's and Calvin Klein products, and explain why they differ")
    check("answered with data", len(t.sql) >= 1 and t.path == "answer", f"path={t.path} sql={len(t.sql)}")
    check("names both brands", "levi" in (t.answer or "").lower() and "calvin" in (t.answer or "").lower())
    check("explains, not just numbers", len(t.answer or "") > 300, f"{len(t.answer or '')} chars")


def s2_top_customers_and_followup():
    """Brief: customer behavior + a genuine follow-up that depends on turn 1."""
    print("\n### 2. top customers, then a follow-up")
    a = Analyst(user_id="mgr_cust", session_id="sc2-" + str(int(time.time())))
    t1 = run(a, "Who are our top 5 customers by total spend?")
    check("returned customers", len(t1.sql) >= 1)
    check("no raw PII in answer", "@" not in (t1.answer or ""), "an e-mail reached the output")
    t2 = run(a, "What did those customers mostly buy?")
    check("follow-up ran its own query", len(t2.sql) >= 1, f"sql={len(t2.sql)} — answered from history")
    low = (t2.answer or "").lower()
    check("no false capability claim", not any(p in low for p in
          ("can't tell you", "cannot tell you", "tools do not allow", "i can only provide")),
          (t2.answer or "")[:150])


def s3_unanswerable_metric():
    """Brief names 'churn rate' — the dataset has no churn column. Must degrade, not invent."""
    print("\n### 3. metric the data cannot support")
    a = Analyst(user_id="mgr_churn", session_id="sc3-" + str(int(time.time())))
    t = run(a, "Why did our churn rate spike last month?")
    low = (t.answer or "").lower()
    flags = ("no direct", "isn't a direct", "not a direct", "no agreed", "define", "definition",
             "proxy", "signal", "clarify", "what you mean")
    check("flags that churn is undefined here", any(f in low for f in flags), (t.answer or "")[:200])
    check("did not crash", t.path in ("answer", "rejected"), f"path={t.path}")
    # The failure to catch is asserting a churn VALUE it never queried. A number inside a proposed
    # definition ("ordered in the last 90 days") is not a claim about the business.
    import re
    values = re.findall(r"\d[\d,.]*\s*%|\$\s?\d[\d,.]*", t.answer or "")
    check("states no metric value without a query behind it", len(t.sql) >= 1 or not values,
          f"claimed {values} with sql=0")


def s4_delete_by_topic():
    """Brief: 'Delete all reports mentioning Client X' — the topic-match variant, not 'this conversation'."""
    print("\n### 4. delete by topic")
    sid = "sc4-" + str(int(time.time()))
    a = Analyst(user_id="mgr_del", session_id=sid)
    run(a, "Show me revenue by month, then save it as a report titled Nike monthly revenue")
    run(a, "Yes, save it")
    run(a, "Now show me our top product categories and save that as a report too")
    run(a, "Yes, save that one")
    before = a.library.list("mgr_del")
    print(f"    library holds {len(before)}: {[r.title[:40] for r in before]}")
    check("two reports saved", len(before) >= 2, f"only {len(before)}")

    t = run(a, "Delete all reports mentioning Nike")
    check("paused for confirmation", bool(t.pending), f"path={t.path} pending={bool(t.pending)}")
    if t.pending:
        print(f"    would delete: {t.pending.report_ids}")
        check("targeted, not everything", len(t.pending.report_ids) < len(before) or len(before) == 1,
              f"{len(t.pending.report_ids)} of {len(before)}")
        a.confirm(t.pending, approve=True)
        after = a.library.list("mgr_del")
        check("survivor kept", len(after) == len(before) - len(t.pending.report_ids),
              f"{len(before)} -> {len(after)}")


def s5_two_managers_isolated():
    """Brief: 'Manager A prefers tables while Manager B prefers bullet points'."""
    print("\n### 5. two managers, separate preferences and libraries")
    ts = str(int(time.time()))
    a = Analyst(user_id="manager_a", session_id="sc5a-" + ts)
    b = Analyst(user_id="manager_b", session_id="sc5b-" + ts)
    run(a, "Always give me tables, never bullet points")
    run(b, "I always want bullet points, never tables")

    ma, mb = a.learned.render("manager_a"), b.learned.render("manager_b")
    print(f"    A learned: {ma!r}")
    print(f"    B learned: {mb!r}")
    check("A's preference stored", "table" in ma.lower())
    check("B's preference stored", "bullet" in mb.lower())
    check("A sees exactly one entry — their own", ma.count("\n") == 0 and mb.count("\n") == 0,
          f"A={ma!r} B={mb!r}")
    check("A's store does not contain B's entry", mb.strip("- ") not in ma, ma)
    check("B's store does not contain A's entry", ma.strip("- ") not in mb, mb)

    run(a, "Show me revenue by month and write it up")
    run(a, "Yes, save it")
    check("B's library does not show A's report", a.library.list("manager_b") == [],
          f"{len(a.library.list('manager_b'))} leaked")
    check("A's library has it", len(a.library.list("manager_a")) >= 1)


def s6_resilience_sql_retry():
    """Brief: 'detect syntax errors or empty returns and attempt to self-correct'.

    Forces the failure by asking for a window the seed trios do not cover, in a phrasing that
    historically produced BigQuery date-type errors.
    """
    print("\n### 6. self-correction on a hard query")
    a = Analyst(user_id="mgr_res", session_id="sc6-" + str(int(time.time())))
    t = run(a, "Show me the running 6-month rolling average of revenue per product category, "
               "week by week, for categories that grew more than 5% year over year")
    check("did not crash the interface", t.path in ("answer", "rejected"), f"path={t.path}")
    check("produced an answer or said why not", bool(t.answer), "empty answer")
    print(f"    queries that succeeded: {len(t.sql)}")
    low = (t.answer or "").lower()
    check("no raw error text reached the user",
          not any(m in low for m in ("traceback", "syntax error at", "badrequest", "exception")),
          (t.answer or "")[:150])


def s7_confirm_from_a_fresh_process():
    """The paused run is reloaded from SQLite, so a different process can confirm it."""
    print("\n### 7. confirmation survives a new Analyst object")
    sid = "sc7-" + str(int(time.time()))
    a = Analyst(user_id="mgr_proc", session_id=sid)
    run(a, "Show me revenue by month and save it as a report")
    run(a, "Yes, save it")
    t = run(a, "Delete every report I have")
    check("paused", bool(t.pending), f"path={t.path}")
    if t.pending:
        fresh = Analyst(user_id="mgr_proc", session_id=sid)      # rebuilt from disk
        out = fresh.confirm(t.pending, approve=True)
        print(f"    resumed elsewhere: {(out.answer or '')[:140]}")
        check("deletion completed after resume", fresh.library.list("mgr_proc") == [],
              f"{len(fresh.library.list('mgr_proc'))} left")


def main() -> int:
    t0 = time.time()
    for fn in (s1_product_comparison, s2_top_customers_and_followup, s3_unanswerable_metric,
               s4_delete_by_topic, s5_two_managers_isolated, s6_resilience_sql_retry,
               s7_confirm_from_a_fresh_process):
        try:
            fn()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAILS.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
    print(f"\n{'='*70}\n{len(FAILS)} failure(s) in {time.time()-t0:.0f}s")
    for f in FAILS:
        print("  ✗", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
