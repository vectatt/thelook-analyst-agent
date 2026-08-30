"""Per-turn traces as JSONL spans: one file per day, one line per span.

Span fields follow the OpenTelemetry GenAI conventions where they apply (operation name, model, token
usage) so the same records can be shipped to Langfuse/OTLP later without changing the emitters.
"""

from __future__ import annotations

import json
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyst.config import settings


def new_trace_id() -> str:
    return secrets.token_hex(4)


class _NullSpan:
    """Stand-in when no tracer is supplied, so callers never branch on `tracer is None`."""
    attrs: dict[str, Any] = {}
    error: str | None = None

    def set(self, **kw: Any) -> None:  # noqa: D102
        pass


@contextmanager
def optional_span(tracer, trace_id: str, name: str, **attrs: Any):
    """Trace if a tracer was given, otherwise do nothing — as a proper context manager.

    Use this rather than calling `span.__enter__()` by hand: `__enter__` returns the span, and calling
    `.set()` on the context manager instead raises at runtime inside a tool, where it surfaces as a
    confusing tool failure.
    """
    if tracer is None:
        yield _NullSpan()
    else:
        with tracer.span(trace_id, name, **attrs) as s:
            yield s


@dataclass
class Span:
    trace_id: str
    name: str
    started: float
    attrs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def set(self, **kw: Any) -> None:
        self.attrs.update({k: v for k, v in kw.items() if v is not None})


class Tracer:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or settings.traces_dir
        self.directory.mkdir(parents=True, exist_ok=True)

    def _file(self) -> Path:
        return self.directory / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def emit(self, span: Span) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": span.trace_id,
            "span": span.name,
            "duration_ms": int((time.perf_counter() - span.started) * 1000),
            "error": span.error,
            **span.attrs,
        }
        with self._file().open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    @contextmanager
    def maybe(self, trace_id: str, name: str, **attrs: Any):
        """`with tracer.maybe(...)` — same as `span`, kept for symmetry with `optional_span`."""
        with self.span(trace_id, name, **attrs) as s:
            yield s

    @contextmanager
    def span(self, trace_id: str, name: str, **attrs: Any):
        s = Span(trace_id=trace_id, name=name, started=time.perf_counter(), attrs=dict(attrs))
        try:
            yield s
        except Exception as e:  # noqa: BLE001 - recorded then re-raised
            s.error = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            self.emit(s)

    # -- reading back ---------------------------------------------------------------------------
    def load(self, trace_id: str) -> list[dict]:
        out: list[dict] = []
        for path in sorted(self.directory.glob("*.jsonl")):
            with path.open() as f:
                for line in f:
                    if trace_id in line:
                        rec = json.loads(line)
                        if rec.get("trace_id") == trace_id:
                            out.append(rec)
        return out

    def recent(self, n: int = 10) -> list[dict]:
        """Last n root ('turn') spans."""
        turns: list[dict] = []
        for path in sorted(self.directory.glob("*.jsonl"), reverse=True):
            with path.open() as f:
                turns.extend(r for r in map(json.loads, f) if r.get("span") == "turn")
            if len(turns) >= n:
                break
        return sorted(turns, key=lambda r: r["ts"], reverse=True)[:n]

    def metrics(self, days: int = 7) -> dict[str, Any]:
        """Agent-level metrics over recent traces — the numbers an on-call person looks at first."""
        recs: list[dict] = []
        for path in sorted(self.directory.glob("*.jsonl"), reverse=True)[:days]:
            with path.open() as f:
                recs.extend(map(json.loads, f))
        turns = [r for r in recs if r["span"] == "turn"]
        sql = [r for r in recs if r["span"] == "bq.run"]
        llm = [r for r in recs if r["span"].startswith("llm.")]
        def rate(xs, pred): return round(100 * sum(1 for x in xs if pred(x)) / len(xs), 1) if xs else None
        lat = sorted(r["duration_ms"] for r in turns)
        # An answer turn that ran no query is a schema question or a library action — worth separating
        # from analyses, because the guardrail path is not the only way a request goes unanswered.
        queried = {r["trace_id"] for r in sql}
        no_query = [t for t in turns if t.get("path") == "answer" and t["trace_id"] not in queried]
        return {
            "turns": len(turns),
            "by_path": {k: sum(1 for t in turns if t.get("path") == k) for k in ("answer", "reports", "rejected", "error")},
            "verified_hit_rate_pct": rate(turns, lambda t: bool(t.get("verified"))),
            "sql_validity_pct": rate(sql, lambda s: not s.get("error")),
            "sql_retries": sum(1 for s in sql if s.get("error")),
            "turn_error_rate_pct": rate(turns, lambda t: bool(t.get("error"))),
            "guardrail_refusal_pct": rate(turns, lambda t: t.get("path") == "rejected"),
            "answered_without_query": len(no_query),
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "llm_calls": len(llm),
            "llm_calls_per_turn": round(len(llm) / len(turns), 2) if turns else None,
            "tokens": sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in llm),
        }
