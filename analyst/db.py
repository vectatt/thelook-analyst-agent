"""Tiny SQLite helper for our own tables (reports, audit, prefs, candidates, turns)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from analyst.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  id          TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  title       TEXT NOT NULL,
  question    TEXT NOT NULL,
  body        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  deleted_at  TEXT
);
CREATE INDEX IF NOT EXISTS reports_owner ON reports(owner, deleted_at);

CREATE TABLE IF NOT EXISTS audit (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  owner       TEXT NOT NULL,
  action      TEXT NOT NULL,
  report_ids  TEXT NOT NULL,
  detail      TEXT
);

CREATE TABLE IF NOT EXISTS prefs (
  user_id     TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS candidates (
  id          TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  question    TEXT NOT NULL,
  sql         TEXT NOT NULL,
  report      TEXT NOT NULL,
  notes       TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL,
  decided_at  TEXT
);

-- what the agent has learned about each manager; free text, human prompt layers always override
CREATE TABLE IF NOT EXISTS learned (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  subject     TEXT NOT NULL,          -- user id
  text        TEXT NOT NULL,
  status      TEXT NOT NULL,          -- active | superseded | forgotten
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learned_lookup ON learned(status, subject);

-- offline judge verdicts, one row per session per metric set
CREATE TABLE IF NOT EXISTS judgements (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  judged_at   TEXT NOT NULL,
  turns       INTEGER NOT NULL,
  metrics     TEXT NOT NULL,          -- json: {metric: bool}
  verdicts    TEXT NOT NULL,          -- json: {judge: {metric: bool}}
  disagreed   INTEGER NOT NULL DEFAULT 0,
  notes       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS judgements_session ON judgements(session_id);

CREATE TABLE IF NOT EXISTS turns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  ts          TEXT NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  trace_id    TEXT,
  sql         TEXT
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, id);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> Path:
    path = path or settings.db_path
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)
    return path


@contextmanager
def connection(path: Path | None = None):
    path = path or settings.db_path
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()
