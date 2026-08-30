"""PII masking on query results, before they reach the agent's context.

This is the layer that runs after execution. It exists because the earlier layers cannot cover
everything: `users_safe` removes the identity columns, and the SQL guard rejects them by name, but a
free-text column the agent legitimately needs — `products.name`, a report body — can still contain an
e-mail address or a phone number that someone typed into the source system.

Masking is applied to the DataFrame, so the agent never sees the raw value and cannot quote it. Column
names are checked too: if a column that looks like an identifier somehow reaches a result, the whole
column is replaced rather than pattern-matched.
"""

from __future__ import annotations

import re

import pandas as pd

from analyst.schema import PII_COLUMNS

# Value patterns. Ordered so the more specific ones run first.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d{1,2}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # POINT(-72.87 -8.06) — geometry that survived into a text column
    ("geo", re.compile(r"\bPOINT\s*\([-\d.\s]+\)", re.I)),
)

REDACTED = "[redacted]"


def mask_text(value: str) -> tuple[str, list[str]]:
    """Mask PII inside one string. Returns the masked text and which kinds were found."""
    found: list[str] = []
    out = value
    for kind, pattern in _PATTERNS:
        out, n = pattern.subn(f"[{kind} {REDACTED}]", out)
        if n:
            found.append(kind)
    return out, found


def mask_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Mask a result set. Returns the masked copy and the columns that were touched.

    Two rules:
      1. a column whose *name* is a known identifier is replaced entirely — no pattern matching,
         because the whole column is sensitive by definition;
      2. every other text column has its values scanned for PII patterns.
    """
    if df is None or df.empty:
        return df, []
    masked = df.copy()
    touched: list[str] = []

    for col in masked.columns:
        if str(col).lower() in PII_COLUMNS:
            masked[col] = REDACTED
            touched.append(str(col))
            continue
        # pandas 3 gives text columns a dedicated `str` dtype, so an `object`-only test would skip
        # every string column — which is exactly where free-text PII hides.
        if not (pd.api.types.is_string_dtype(masked[col]) or masked[col].dtype == object):
            continue
        hits = False

        def _apply(v):
            nonlocal hits
            if not isinstance(v, str):
                return v
            out, found = mask_text(v)
            if found:
                hits = True
            return out

        masked[col] = masked[col].map(_apply)
        if hits:
            touched.append(str(col))

    return masked, touched
