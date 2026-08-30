"""Pure tests for the SQL guard — no network, no model."""

import pytest

from analyst.safety.sql_guard import SqlGuardError, guard_sql

SAFE = "proj-x.thelook_safe"


def g(sql: str, **kw):
    kw.setdefault("use_safe_views", True)
    kw.setdefault("safe_dataset", SAFE)
    kw.setdefault("default_limit", 200)
    kw.setdefault("max_limit", 1000)
    return guard_sql(sql, **kw)


def test_rewrites_tables_to_safe_dataset_and_adds_limit():
    out = g("SELECT status, COUNT(*) n FROM orders GROUP BY status")
    assert "`proj-x`.thelook_safe.orders" in out.sql
    assert out.limit == 200 and "LIMIT 200" in out.sql
    assert out.tables == ["orders"]


def test_users_becomes_users_safe_and_keeps_alias():
    out = g("SELECT u.state, COUNT(*) FROM users u JOIN order_items oi ON oi.user_id = u.id GROUP BY 1")
    assert "users_safe AS u" in out.sql
    assert sorted(out.tables) == ["order_items", "users_safe"]


def test_public_dataset_reference_is_redirected():
    out = g("SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.order_items`")
    assert "bigquery-public-data" not in out.sql
    assert "thelook_safe.order_items" in out.sql


@pytest.mark.parametrize("col", ["email", "first_name", "street_address", "latitude", "user_geom", "postal_code"])
def test_pii_columns_are_rejected(col):
    with pytest.raises(SqlGuardError, match="personal data"):
        g(f"SELECT id, {col} FROM users")


def test_pii_rejected_even_in_where_or_subquery():
    with pytest.raises(SqlGuardError):
        g("SELECT id FROM users WHERE email LIKE '%@x.com'")
    with pytest.raises(SqlGuardError):
        g("SELECT * FROM orders WHERE user_id IN (SELECT id FROM users WHERE last_name = 'Smith')")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders WHERE 1=1",
        "UPDATE products SET cost = 0",
        "INSERT INTO orders SELECT * FROM orders",
        "DROP TABLE products",
        "CREATE TABLE t AS SELECT 1",
        "SELECT 1; SELECT 2",
    ],
)
def test_non_select_is_rejected(sql):
    with pytest.raises(SqlGuardError):
        g(sql)


def test_unknown_table_is_rejected():
    with pytest.raises(SqlGuardError, match="not available"):
        g("SELECT * FROM events")
    with pytest.raises(SqlGuardError, match="not available"):
        g("SELECT * FROM `bigquery-public-data.thelook_ecommerce.inventory_items`")


def test_cte_names_are_not_treated_as_tables():
    out = g("WITH rev AS (SELECT user_id, SUM(sale_price) r FROM order_items GROUP BY 1) SELECT * FROM rev ORDER BY r DESC")
    assert out.tables == ["order_items"]


def test_limit_is_clamped():
    out = g("SELECT * FROM products LIMIT 50000")
    assert out.limit == 1000 and "LIMIT 1000" in out.sql


def test_inline_projection_when_views_missing():
    out = g("SELECT u.state, COUNT(*) FROM users u GROUP BY 1", use_safe_views=False)
    assert "bigquery-public-data" in out.sql
    assert "email" not in out.sql and "street_address" not in out.sql
    flat = " ".join(out.sql.split())
    assert "SELECT id, age, gender, city, state, country, traffic_source, created_at FROM" in flat
