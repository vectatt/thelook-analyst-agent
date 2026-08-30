"""PII masking on result sets — pure, no network.

The layer that catches what the views and the SQL guard cannot: personal data sitting inside a
free-text column the agent legitimately queried.
"""

import pandas as pd
import pytest

from analyst.safety.masking import REDACTED, mask_dataframe, mask_text


def test_free_text_column_is_scanned():
    """Regression: pandas 3 uses a `str` dtype, so an `object`-only check skipped every string column."""
    df = pd.DataFrame([{"product": "Lee Jeans, contact bulk@lee.com or 555-321-9987", "revenue": 1200.0}])
    out, cols = mask_dataframe(df)
    assert cols == ["product"]
    assert "bulk@lee.com" not in out.iloc[0]["product"]
    assert "555-321-9987" not in out.iloc[0]["product"]
    assert out.iloc[0]["revenue"] == 1200.0          # numbers must survive untouched


def test_identifier_column_is_replaced_wholesale():
    df = pd.DataFrame([{"id": 1, "email": "a@b.com", "first_name": "Michael", "state": "Texas"}])
    out, cols = mask_dataframe(df)
    assert set(cols) == {"email", "first_name"}
    assert out.iloc[0]["email"] == REDACTED and out.iloc[0]["first_name"] == REDACTED
    assert out.iloc[0]["state"] == "Texas" and out.iloc[0]["id"] == 1


@pytest.mark.parametrize("raw, kind", [
    ("write to john.doe@example.net today", "email"),
    ("call 555-321-9987 now", "phone"),
    ("card 4532 1234 5678 9012", "card"),
    ("ssn 123-45-6789", "ssn"),
    ("home POINT(-72.87094866 -8.065346116)", "geo"),
    ("server 192.168.1.24", "ip"),
])
def test_patterns(raw, kind):
    out, found = mask_text(raw)
    assert kind in found and REDACTED in out


def test_ordinary_text_is_left_alone():
    for clean in ["Outerwear & Coats", "revenue grew 18% in Q1 2026", "top 3 brands", "rpt_1a2b3c"]:
        out, found = mask_text(clean)
        assert found == [] and out == clean


def test_empty_and_numeric_frames_are_safe():
    assert mask_dataframe(pd.DataFrame())[1] == []
    df = pd.DataFrame([{"month": "2026-01", "revenue": 1000}])
    out, cols = mask_dataframe(df)
    assert cols == [] and out.equals(df)
