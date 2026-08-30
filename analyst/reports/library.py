"""Saved-reports library: the only thing in the system the agent can destroy.

Ownership is enforced in SQL (`WHERE owner = ?`), deletes are soft (`deleted_at`) so the audit trail
survives them, and every mutation writes an audit row. `delete` is idempotent: already-deleted ids are
ignored, which matters because a resumed run may execute the tool more than once.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from analyst.db import connection, init_db



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Report:
    id: str
    owner: str
    session_id: str
    title: str
    question: str
    body: str
    description: str
    created_at: str
    deleted_at: str | None = None

    @property
    def created_date(self) -> str:
        return self.created_at[:10]


class ReportLibrary:
    def __init__(self):
        init_db()

    # -- write ------------------------------------------------------------------------------------
    def save(self, owner: str, session_id: str, title: str, question: str, body: str,
             description: str = "") -> Report:
        rid = "rpt_" + secrets.token_hex(3)
        now = _now()
        with connection() as c:
            c.execute(
                "INSERT INTO reports (id, owner, session_id, title, question, body, description, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rid, owner, session_id, title, question, body, description, now),
            )
            self._audit(c, owner, "save", [rid], title)
        return Report(rid, owner, session_id, title, question, body, description, now)

    def delete(self, owner: str, ids: list[str]) -> list[str]:
        """Soft-delete the caller's own reports. Returns the ids actually deleted (idempotent)."""
        now = _now()
        with connection() as c:
            rows = c.execute(
                f"SELECT id FROM reports WHERE owner = ? AND deleted_at IS NULL AND id IN ({','.join('?' * len(ids))})",
                (owner, *ids),
            ).fetchall() if ids else []
            hit = [r["id"] for r in rows]
            if hit:
                c.execute(
                    f"UPDATE reports SET deleted_at = ? WHERE id IN ({','.join('?' * len(hit))})", (now, *hit)
                )
                self._audit(c, owner, "delete", hit, None)
        return hit

    # -- read -------------------------------------------------------------------------------------
    def list(self, owner: str, include_deleted: bool = False) -> list[Report]:
        with connection() as c:
            sql = "SELECT * FROM reports WHERE owner = ?" + ("" if include_deleted else " AND deleted_at IS NULL")
            return [Report(**dict(r)) for r in c.execute(sql + " ORDER BY created_at DESC", (owner,)).fetchall()]

    def find(self, owner: str, text: str | None = None, session_id: str | None = None) -> list[Report]:
        """Case-insensitive substring match over title, question and body, scoped to the owner."""
        with connection() as c:
            sql = "SELECT * FROM reports WHERE owner = ? AND deleted_at IS NULL"
            args: list = [owner]
            if text:
                like = f"%{text.lower()}%"
                sql += " AND (LOWER(title) LIKE ? OR LOWER(description) LIKE ? OR LOWER(question) LIKE ? OR LOWER(body) LIKE ?)"
                args += [like, like, like, like]
            if session_id:
                sql += " AND session_id = ?"; args.append(session_id)
            return [Report(**dict(r)) for r in c.execute(sql + " ORDER BY created_at DESC", args).fetchall()]

    def get(self, owner: str, rid: str) -> Report | None:
        with connection() as c:
            r = c.execute("SELECT * FROM reports WHERE owner = ? AND id = ?", (owner, rid)).fetchone()
            return Report(**dict(r)) if r else None

    def audit_log(self, owner: str, limit: int = 20) -> list[dict]:
        with connection() as c:
            return [dict(r) for r in c.execute(
                "SELECT ts, action, report_ids, detail FROM audit WHERE owner = ? ORDER BY id DESC LIMIT ?", (owner, limit)
            ).fetchall()]

    @staticmethod
    def _audit(c, owner: str, action: str, ids: list[str], detail: str | None) -> None:
        c.execute("INSERT INTO audit (ts, owner, action, report_ids, detail) VALUES (?,?,?,?,?)",
                  (_now(), owner, action, json.dumps(ids), detail))
