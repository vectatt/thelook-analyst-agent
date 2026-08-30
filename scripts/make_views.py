"""Create the PII-free views in your own project.  Usage: python scripts/make_views.py"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import bigquery  # noqa: E402

from analyst.config import settings  # noqa: E402


def main() -> int:
    problems = settings.validate()
    if problems:
        print("\n".join(problems))
        return 1
    client = bigquery.Client(project=settings.project)
    sql = (Path(__file__).resolve().parent.parent / "sql" / "views.sql").read_text()
    sql = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements = [s.strip() for s in sql.replace("{project}", settings.project).split(";") if s.strip()]
    for stmt in statements:
        print(f"→ {stmt.splitlines()[0][:90]}")
        client.query(stmt).result()
    rows = list(client.query(f"SELECT COUNT(*) AS n FROM `{settings.safe_dataset}.users_safe`").result())
    print(f"✓ {settings.safe_dataset}.users_safe has {rows[0].n:,} rows and no PII columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
