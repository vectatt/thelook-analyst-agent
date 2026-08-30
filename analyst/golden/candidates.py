"""Candidate queue: accepted answers waiting for an analyst to promote them into the golden bucket."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from analyst.db import connection
from analyst.golden.models import Trio
from analyst.golden.store import GoldenIndex


def list_pending() -> list[dict]:
    with connection() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, owner, question, created_at FROM candidates WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()]


def get(cid: str) -> dict | None:
    with connection() as c:
        r = c.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone()
        return dict(r) if r else None


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:48] or "candidate"


def approve(cid: str, index: GoldenIndex, *, trio_id: str | None = None, notes: str | None = None) -> Trio:
    """Promote a candidate: write it as a trio YAML, re-index, mark approved. Human action only."""
    cand = get(cid)
    if not cand or cand["status"] != "pending":
        raise ValueError("no such pending candidate")
    sql = cand["sql"].split("\n---\n")[0].strip()          # first step only; multi-step answers need an analyst's edit
    tid = trio_id or _slug(cand["question"])
    if tid in index.trios:
        tid = f"{tid}_{cid[-4:]}"
    trio = Trio(id=tid, question=cand["question"], sql=sql, report=cand["report"],
                analyst_notes=notes or cand.get("notes") or "Promoted from an accepted answer; review the notes.",
                tags=["promoted"], verified_at=date.today(), source=f"candidate:{cid}")
    index.add(trio)
    with connection() as c:
        c.execute("UPDATE candidates SET status = 'approved', decided_at = ? WHERE id = ?",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds"), cid))
    return trio


def reject(cid: str) -> None:
    with connection() as c:
        c.execute("UPDATE candidates SET status = 'rejected', decided_at = ? WHERE id = ?",
                  (datetime.now(timezone.utc).isoformat(timespec="seconds"), cid))


def queue(*, owner: str, session_id: str, question: str, sql: str, report: str, notes: str = "") -> str:
    """Queue an approved report as a candidate trio. Human promotion is still required."""
    import secrets
    from datetime import datetime, timezone
    cid = "cand_" + secrets.token_hex(3)
    with connection() as c:
        c.execute(
            "INSERT INTO candidates (id, owner, session_id, question, sql, report, notes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, owner, session_id, question, sql, report, notes,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    return cid
