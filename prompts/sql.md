# How to write SQL against this warehouse — owned by engineering.

- SELECT only, one statement per query. Never DML or DDL; the connection is read-only.
- Round money to 2 decimals in SQL. Use SAFE_DIVIDE for every ratio.
- For a comparison, return BOTH sides in ONE query with a column identifying the side.
- For a "why" question, decompose the metric so the driver is visible in the columns — revenue per
  customer, items per customer, average item price — not just a total.
- Never select or filter on names, e-mails, addresses, postal codes or coordinates. They do not exist
  in these tables and the query will be rejected.
- Add an explicit LIMIT for row-level lists.
- Prefer one query that answers the whole question over several partial ones.
- **Return the totals and ratios the answer will quote**, do not leave them to be worked out later. If
  the question asks for a monthly breakdown per brand, return the per-brand total too — with a window
  function (`SUM(...) OVER (PARTITION BY brand)`) or a `GROUP BY ROLLUP`. A figure the writer has to
  calculate itself cannot be traced to a row.

## Dates — the most common cause of a failed query here

Every `*_at` column (`created_at`, `shipped_at`, `delivered_at`, `returned_at`) is a **TIMESTAMP**, and
BigQuery is strict about mixing timestamp and date arithmetic. Follow these exactly:

- **Month, quarter or year arithmetic must go through `DATE()`**:
  `DATE(oi.created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 6 MONTH)`
  `TIMESTAMP_SUB` supports only MICROSECOND … DAY — `TIMESTAMP_SUB(x, INTERVAL 6 MONTH)` is an error.
- **Group by month** with `DATE_TRUNC(DATE(oi.created_at), MONTH)`, never `DATE_TRUNC(oi.created_at, MONTH)`.
- **Complete months only**, so a partial current month does not read as a collapse:
  `DATE(oi.created_at) >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 6 MONTH)`
  `AND DATE(oi.created_at) < DATE_TRUNC(CURRENT_DATE(), MONTH)`
- Never compare a TIMESTAMP column directly to a DATE expression; wrap the column in `DATE()` first.
