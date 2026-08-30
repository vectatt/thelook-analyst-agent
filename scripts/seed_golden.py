"""Verify every trio executes against BigQuery, then (re)build the golden-bucket index.

Usage: python scripts/seed_golden.py            # verify + index
       python scripts/seed_golden.py --no-run   # index only (no BigQuery)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyst.config import settings  # noqa: E402
from analyst.golden.models import load_trios  # noqa: E402


def verify(trios) -> int:
    from analyst.bq.tool import QueryError, SafeBigQuery, SqlGuardError

    bq = SafeBigQuery()
    failures = 0
    for t in trios:
        try:
            res = bq.run(t.sql)
            flag = "" if res.rows else "   (0 rows — check the window)"
            print(f"  ✓ {t.id:32s} {res.rows:4d} rows  {res.bytes_processed/1e6:6.1f} MB  {res.elapsed_ms:5d} ms{flag}")
        except (QueryError, SqlGuardError) as e:
            failures += 1
            print(f"  ✗ {t.id:32s} {type(e).__name__}: {str(e)[:120]}")
    return failures


def main() -> int:
    problems = settings.validate()
    if problems:
        print("\n".join(problems)); return 1
    trios = load_trios(settings.golden_dir)
    print(f"{len(trios)} trios")
    if "--no-run" not in sys.argv:
        failures = verify(trios)
        if failures:
            print(f"\n{failures} trio(s) failed — fix them before indexing."); return 1

    from analyst.golden.store import GoldenIndex

    idx = GoldenIndex()
    idx.rebuild()
    print(f"\nindexed {len(idx.trios)} trios into {settings.lancedb_path}")
    for q in ["monthly revenue", "why are customers in Tennessee underspending vs South Carolina?",
              "how is Texas doing this year", "what colour is the sky"]:
        matches, degraded = idx.search(q, k=1)
        best = matches[0] if matches else None
        verdict = "replay" if best and best.score >= settings.tau_replay else "notes only"
        print(f"  {q[:52]:52s} → {verdict:10s} best={best.score:.2f} {best.trio.id}" if best else
              f"  {q[:52]:52s} → no match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
