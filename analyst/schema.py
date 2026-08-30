"""What the model is allowed to know about the data.

The schema below describes the *safe views*, not the raw public tables. `users_safe` omits every
direct and quasi-identifier; the other three views are pass-throughs. The PII deny-list is enforced
independently by the SQL guard, so even a model that guesses a column name cannot select it.
"""

from __future__ import annotations

from dataclasses import dataclass

PII_COLUMNS: frozenset[str] = frozenset(
    {"first_name", "last_name", "email", "street_address", "postal_code", "latitude", "longitude", "user_geom"}
)

USERS_SAFE_COLUMNS: tuple[str, ...] = ("id", "age", "gender", "city", "state", "country", "traffic_source", "created_at")


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    note: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]


TABLES: dict[str, Table] = {
    "users_safe": Table(
        "users_safe",
        "One row per customer. Identity columns are intentionally absent; refer to customers by id.",
        (
            Column("id", "INT64", "customer id; join key for orders.user_id / order_items.user_id"),
            Column("age", "INT64", "note: minors appear in this synthetic dataset"),
            Column("gender", "STRING", "'M' or 'F'"),
            Column("city", "STRING", "can be the literal string 'null'"),
            Column("state", "STRING", "full state / region name, e.g. 'California', 'Acre'"),
            Column("country", "STRING", "e.g. 'United States', 'Brasil', 'China'"),
            Column("traffic_source", "STRING", "acquisition channel: Search, Organic, Email, Facebook, Display"),
            Column("created_at", "TIMESTAMP", "account creation"),
        ),
    ),
    "orders": Table(
        "orders",
        "One row per order header.",
        (
            Column("order_id", "INT64"),
            Column("user_id", "INT64"),
            Column("status", "STRING", "Complete, Shipped, Processing, Cancelled, Returned"),
            Column("gender", "STRING", "denormalised copy of the customer's gender"),
            Column("created_at", "TIMESTAMP", "order placed"),
            Column("returned_at", "TIMESTAMP"),
            Column("shipped_at", "TIMESTAMP"),
            Column("delivered_at", "TIMESTAMP"),
            Column("num_of_item", "INT64", "items in the order — per ORDER, never multiply by sale_price"),
        ),
    ),
    "order_items": Table(
        "order_items",
        "One row per item sold. This is the revenue fact table.",
        (
            Column("id", "INT64"),
            Column("order_id", "INT64"),
            Column("user_id", "INT64"),
            Column("product_id", "INT64"),
            Column("inventory_item_id", "INT64"),
            Column("status", "STRING", "same values as orders.status"),
            Column("created_at", "TIMESTAMP"),
            Column("shipped_at", "TIMESTAMP"),
            Column("delivered_at", "TIMESTAMP"),
            Column("returned_at", "TIMESTAMP"),
            Column("sale_price", "FLOAT64", "price paid for THIS item; revenue = SUM(sale_price)"),
        ),
    ),
    "products": Table(
        "products",
        "Product catalogue.",
        (
            Column("id", "INT64", "join key for order_items.product_id"),
            Column("cost", "FLOAT64", "unit cost; margin = sale_price - cost"),
            Column("category", "STRING", "e.g. Jeans, Accessories, Swim"),
            Column("name", "STRING"),
            Column("brand", "STRING"),
            Column("retail_price", "FLOAT64", "list price"),
            Column("department", "STRING", "'Men' or 'Women'"),
            Column("sku", "STRING"),
            Column("distribution_center_id", "INT64"),
        ),
    ),
}

# logical names the model may use; `users` is accepted and rewritten to `users_safe` by the guard
ALLOWED_TABLES: frozenset[str] = frozenset(TABLES) | {"users"}

ANALYST_CONVENTIONS = """\
- Revenue = SUM(order_items.sale_price). Exclude status IN ('Cancelled', 'Returned') unless the question is about cancellations or returns.
- Never multiply sale_price by orders.num_of_item: sale_price is per item, num_of_item is per order.
- Margin = sale_price - products.cost. Do not use retail_price for revenue.
- Compare regions per customer (revenue / COUNT(DISTINCT user_id)), never on raw totals: populations differ.
- Dates: the dataset is continuously regenerated and contains future-dated rows. Use CURRENT_DATE()-relative windows,
  and say which window you used. "Last month" = the previous calendar month.
- Join order_items.user_id = users_safe.id, order_items.product_id = products.id, order_items.order_id = orders.order_id.
- Refer to customers only by id and segment (state, age band, channel). Never request names, e-mails or addresses.
"""


def schema_markdown() -> str:
    """Compact schema block for prompts."""
    lines = []
    for table in TABLES.values():
        lines.append(f"### {table.name} — {table.description}")
        for c in table.columns:
            note = f"  -- {c.note}" if c.note else ""
            lines.append(f"- {c.name} {c.type}{note}")
    return "\n".join(lines)


def schema_overview() -> str:
    """Plain-language answer for 'what data do we have?' — no model, no query."""
    parts = ["I have four tables from the TheLook e-commerce dataset (read-only, customer identities removed):", ""]
    for table in TABLES.values():
        cols = ", ".join(c.name for c in table.columns)
        parts.append(f"• {table.name} — {table.description}\n  columns: {cols}")
    parts.append("")
    # Spelling out the joins matters: without them the model reads a previous result's columns as the
    # limit of what exists and tells the user something is unavailable when it is one join away.
    parts.append(
        "How they join — every table reaches every other, so almost any combination is answerable:\n"
        "  users_safe.id = orders.user_id = order_items.user_id\n"
        "  orders.order_id = order_items.order_id\n"
        "  order_items.product_id = products.id\n"
        "So: what a given customer bought (users_safe → order_items → products), revenue by any "
        "customer attribute, product performance by region or age band, and so on."
    )
    parts.append("")
    parts.append("Things you can ask: revenue and orders over time, top products/brands/categories, margin, "
                 "customer segments by state/age/channel, cancellations and returns, comparisons between "
                 "two products or regions and why they differ, and a full report with action items.")
    return "\n".join(parts)
