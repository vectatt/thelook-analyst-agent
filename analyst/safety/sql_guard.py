"""Static SQL guard: the last line of defence before a query reaches BigQuery.

Guarantees, in order:
1. exactly one statement, and it is a SELECT (or UNION of SELECTs) — no DML/DDL/scripting;
2. every physical table is one of the four allowed logical tables; anything else is rejected.
   A CTE alias only shadows *unqualified* references: `WITH users AS (...) SELECT * FROM users` is
   fine, but the table inside the CTE body is checked like any other, and a qualified reference
   such as `project.dataset.users` is never treated as the CTE;
3. no PII column is referenced anywhere in the statement, by name;
4. every table reference is rewritten to the safe dataset (`<project>.thelook_safe.<view>`), so the
   query can only touch the views the credential is granted on. `users` becomes `users_safe`.
   If the safe views are not available, `users` is replaced by an inline projection of the safe
   columns over the public table, which gives the same column-level guarantee at query time;
5. the outermost SELECT has an integer LIMIT, clamped to `max_limit`.

Every parser or shape problem surfaces as `SqlGuardError` with a message the repair step can act on.
`analyst.bq.tool.SafeBigQuery` adds a second, independent check after the dry run: BigQuery's own
`statement_type` and `referenced_tables` must agree with what the guard allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from analyst.config import SOURCE_DATASET, settings
from analyst.schema import ALLOWED_TABLES, PII_COLUMNS, USERS_SAFE_COLUMNS

DIALECT = "bigquery"

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command,
    exp.TruncateTable, exp.Grant, exp.Set, exp.Transaction, exp.Commit, exp.Rollback,
)


class SqlGuardError(ValueError):
    """Raised with a message the model can act on (it is fed back to the repair step)."""


@dataclass
class GuardedSql:
    sql: str
    tables: list[str] = field(default_factory=list)
    limit: int | None = None
    rewrites: list[str] = field(default_factory=list)


def _parse(sql: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except SqlglotError as e:
        raise SqlGuardError(f"SQL does not parse as BigQuery SQL: {str(e)[:300]}") from e
    except Exception as e:  # noqa: BLE001 - sqlglot can raise TypeError/RecursionError on odd input
        raise SqlGuardError(f"SQL could not be parsed: {type(e).__name__}: {str(e)[:200]}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlGuardError(f"Exactly one statement is allowed, got {len(statements)}.")
    return statements[0]


def _assert_read_only(tree: exp.Expression) -> None:
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise SqlGuardError(f"Only SELECT queries are allowed, got {type(tree).__name__}.")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlGuardError(f"Statement contains a forbidden {type(node).__name__} node.")


def _assert_no_pii(tree: exp.Expression) -> None:
    hits = sorted({c.name for c in tree.find_all(exp.Column) if c.name.lower() in PII_COLUMNS})
    if hits:
        raise SqlGuardError(
            f"Columns {hits} are personal data and cannot be queried. "
            f"Refer to customers by id and by segment (state, age, traffic_source) instead."
        )


def _is_qualified(table: exp.Table) -> bool:
    return bool(table.db or table.catalog)


def _safe_table(logical: str, safe_dataset: str) -> exp.Expression:
    view = "users_safe" if logical == "users" else logical
    project, dataset = safe_dataset.split(".", 1)
    return exp.Table(
        this=exp.to_identifier(view),
        db=exp.to_identifier(dataset),
        catalog=exp.to_identifier(project, quoted=True),
    )


def _inline_users(alias: str | None) -> exp.Expression:
    """`(SELECT id, age, ... FROM public.users) AS alias` — used when the safe views do not exist."""
    cols = ", ".join(USERS_SAFE_COLUMNS)
    sub = sqlglot.parse_one(f"SELECT {cols} FROM `{SOURCE_DATASET}.users`", read=DIALECT)
    return exp.Subquery(this=sub, alias=exp.to_identifier(alias or "users"))


def _public_table(logical: str) -> exp.Expression:
    project, dataset = SOURCE_DATASET.split(".", 1)
    return exp.Table(
        this=exp.to_identifier(logical),
        db=exp.to_identifier(dataset),
        catalog=exp.to_identifier(project, quoted=True),
    )


def _rewrite_tables(tree: exp.Expression, use_safe_views: bool, safe_dataset: str) -> tuple[list[str], list[str]]:
    seen: list[str] = []
    rewrites: list[str] = []
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    for table in list(tree.find_all(exp.Table)):
        logical = table.name.lower()
        if logical in cte_names and not _is_qualified(table):
            continue  # unqualified reference to a WITH clause; the CTE body itself is still checked
        if logical == "users_safe":
            logical = "users"
        if logical not in ALLOWED_TABLES:
            raise SqlGuardError(
                f"Table '{table.sql(dialect=DIALECT)}' is not available. "
                f"Use only: users, orders, order_items, products."
            )
        seen.append("users_safe" if logical == "users" else logical)
        alias = table.alias or None

        if use_safe_views:
            replacement = _safe_table(logical, safe_dataset)
            if alias:
                replacement = exp.alias_(replacement, alias, table=False)
            elif logical == "users":
                replacement = exp.alias_(replacement, "users", table=False)  # keep `users.<col>` valid
        elif logical == "users":
            replacement = _inline_users(alias)
        else:
            replacement = _public_table(logical)
            if alias:
                replacement = exp.alias_(replacement, alias, table=False)

        rewrites.append(f"{table.sql(dialect=DIALECT)} -> {replacement.sql(dialect=DIALECT)}")
        table.replace(replacement)
    return sorted(set(seen)), rewrites


def _enforce_limit(tree: exp.Expression, default_limit: int, max_limit: int) -> int:
    limit_node = tree.args.get("limit")
    if limit_node is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(default_limit)))
        return default_limit
    expression = limit_node.expression
    if not isinstance(expression, exp.Literal) or not expression.is_int:
        raise SqlGuardError("LIMIT must be a plain integer literal, e.g. LIMIT 100.")
    current = int(expression.this)
    if current > max_limit:
        limit_node.set("expression", exp.Literal.number(max_limit))
        return max_limit
    return current


def guard_sql(
    sql: str,
    *,
    use_safe_views: bool | None = None,
    safe_dataset: str | None = None,
    default_limit: int | None = None,
    max_limit: int | None = None,
) -> GuardedSql:
    """Validate and rewrite `sql`. Raises SqlGuardError with an actionable message."""
    use_safe_views = settings.use_safe_views if use_safe_views is None else use_safe_views
    safe_dataset = safe_dataset or settings.safe_dataset
    default_limit = default_limit or settings.default_limit
    max_limit = max_limit or settings.max_limit

    tree = _parse(sql.strip().rstrip(";"))
    _assert_read_only(tree)
    _assert_no_pii(tree)
    try:
        tables, rewrites = _rewrite_tables(tree, use_safe_views, safe_dataset)
        limit = _enforce_limit(tree, default_limit, max_limit)
        text = tree.sql(dialect=DIALECT, pretty=True)
    except SqlGuardError:
        raise
    except Exception as e:  # noqa: BLE001 - never let a parser corner case escape as a raw exception
        raise SqlGuardError(f"SQL could not be validated: {type(e).__name__}: {str(e)[:200]}") from e
    return GuardedSql(sql=text, tables=tables, limit=limit, rewrites=rewrites)


def allowed_physical_tables(use_safe_views: bool | None = None, safe_dataset: str | None = None) -> set[str]:
    """Fully-qualified tables a guarded query may reference — used by the post-dry-run check."""
    use_safe_views = settings.use_safe_views if use_safe_views is None else use_safe_views
    safe_dataset = safe_dataset or settings.safe_dataset
    if use_safe_views:
        return {f"{safe_dataset}.{t}" for t in ("users_safe", "orders", "order_items", "products")}
    return {f"{SOURCE_DATASET}.{t}" for t in ("users", "orders", "order_items", "products")}
