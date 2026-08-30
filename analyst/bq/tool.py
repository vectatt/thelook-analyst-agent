"""Safe BigQuery execution: guard → dry-run → byte cap → execute.

Wraps the provided `bq_client.BigQueryRunner` so the same client object is used, while adding the
controls the raw runner lacks. Nothing here talks to a language model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd
from google.api_core import exceptions as gexc
from google.cloud import bigquery

from analyst.config import settings
from analyst.safety.sql_guard import GuardedSql, SqlGuardError, guard_sql
from bq_client import BigQueryRunner


class QueryError(RuntimeError):
    """A BigQuery-side failure with a message safe to feed back to the SQL repair step."""

    def __init__(self, message: str, *, kind: str, retryable: bool = False):
        super().__init__(message)
        self.kind = kind            # syntax | cost | not_found | unavailable | other
        self.retryable = retryable


@dataclass
class DryRun:
    bytes_processed: int
    guarded: GuardedSql


@dataclass
class QueryResult:
    sql: str
    df: pd.DataFrame
    rows: int
    bytes_processed: int
    elapsed_ms: int
    job_id: str | None
    truncated: bool = False
    tables: list[str] = field(default_factory=list)

    @property
    def bytes_scanned_mb(self) -> float:
        return self.bytes_processed / 1e6

    def preview(self, max_rows: int | None = None) -> str:
        """Bounded, model-facing rendering of the result."""
        n = max_rows or settings.rows_to_model
        head = self.df.head(n)
        text = head.to_markdown(index=False) if len(head) else "(no rows)"
        if self.rows > n:
            text += f"\n... {self.rows - n} more rows not shown"
        return text


def _classify(exc: Exception) -> QueryError:
    msg = str(exc)
    if isinstance(exc, gexc.BadRequest):
        low = msg.lower()
        if "bytes" in low and "billed" in low:
            return QueryError(msg, kind="cost")
        if "not found" in low or "unrecognized name" in low:
            return QueryError(msg, kind="not_found")
        return QueryError(msg, kind="syntax")
    if isinstance(exc, gexc.NotFound):
        return QueryError(msg, kind="not_found")
    if isinstance(exc, (gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.TooManyRequests, gexc.InternalServerError)):
        return QueryError(msg, kind="unavailable", retryable=True)
    return QueryError(msg, kind="other")


class SafeBigQuery:
    def __init__(self, project: str | None = None, max_bytes_billed: int | None = None):
        self.runner = BigQueryRunner(project_id=project or settings.project)
        self.client: bigquery.Client = self.runner.client
        self.max_bytes_billed = max_bytes_billed or settings.max_bytes_billed

    # -- checks -----------------------------------------------------------------------------------
    def dry_run(self, sql: str) -> DryRun:
        """Guard + BigQuery dry run. Costs nothing; surfaces syntax errors and the bytes a run would scan."""
        guarded = guard_sql(sql)
        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self.client.query(guarded.sql, job_config=cfg)
        except Exception as e:  # noqa: BLE001 - classified below
            raise _classify(e) from e
        return DryRun(bytes_processed=int(job.total_bytes_processed or 0), guarded=guarded)

    # -- execution --------------------------------------------------------------------------------
    def run(self, sql: str) -> QueryResult:
        """Guard, dry-run, enforce the byte cap, execute, and return a bounded result."""
        dry = self.dry_run(sql)
        if dry.bytes_processed > self.max_bytes_billed:
            raise QueryError(
                f"Query would scan {dry.bytes_processed / 1e6:.1f} MB, above the {self.max_bytes_billed / 1e6:.0f} MB cap. "
                f"Narrow the date window or select fewer columns.",
                kind="cost",
            )
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=self.max_bytes_billed)
        started = time.perf_counter()
        try:
            job = self.client.query(dry.guarded.sql, job_config=cfg)
            df = job.result().to_dataframe()
        except Exception as e:  # noqa: BLE001
            raise _classify(e) from e
        elapsed = int((time.perf_counter() - started) * 1000)
        return QueryResult(
            sql=dry.guarded.sql,
            df=df,
            rows=len(df),
            bytes_processed=int(job.total_bytes_processed or dry.bytes_processed),
            elapsed_ms=elapsed,
            job_id=job.job_id,
            truncated=dry.guarded.limit is not None and len(df) >= dry.guarded.limit,
            tables=dry.guarded.tables,
        )

    def safe_views_exist(self) -> bool:
        try:
            self.client.get_table(f"{settings.safe_dataset}.users_safe")
            return True
        except Exception:  # noqa: BLE001
            return False


__all__ = ["SafeBigQuery", "QueryResult", "QueryError", "SqlGuardError", "DryRun"]
