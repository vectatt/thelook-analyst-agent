"""Regression tests for the code-review findings — pure, no network."""

import pytest

from analyst.safety.sql_guard import SqlGuardError, guard_sql

SAFE = "proj-x.thelook_safe"


def g(sql, **kw):
    kw.setdefault("use_safe_views", True); kw.setdefault("safe_dataset", SAFE)
    kw.setdefault("default_limit", 200); kw.setdefault("max_limit", 1000)
    return guard_sql(sql, **kw)


# -- CTE shadowing must not bypass the allow-list ----------------------------------------------
def test_cte_body_tables_are_still_checked():
    with pytest.raises(SqlGuardError, match="not available"):
        g("WITH t AS (SELECT * FROM `other-proj.ds.t`) SELECT * FROM t")


def test_cte_named_like_a_table_cannot_smuggle_the_raw_public_table():
    out = g("WITH users AS (SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`) SELECT id FROM users")
    # the CTE body's physical table was rewritten to the safe view; the raw table never appears
    assert "bigquery-public-data" not in out.sql
    assert "users_safe" in out.sql and out.tables == ["users_safe"]


def test_qualified_reference_is_never_treated_as_cte():
    with pytest.raises(SqlGuardError, match="not available"):
        g("WITH t AS (SELECT 1) SELECT * FROM `other-proj.ds.t`")


# -- parser corner cases surface as SqlGuardError, never raw exceptions --------------------------
@pytest.mark.parametrize("sql", ["SELECT * FROM `products", "SELECT * FROM products LIMIT 2*5", "SELECT * FROM products LIMIT x"])
def test_parser_and_limit_problems_are_guard_errors(sql):
    with pytest.raises(SqlGuardError):
        g(sql)


# -- prompt layers: the persona must be changeable without touching code -------------------------
def test_prompt_layers_compose_and_version(tmp_path):
    from analyst import prompts as P

    (tmp_path / "persona.md").write_text("---\nmax_words: 120\n---\nBe terse.")
    (tmp_path / "report.md").write_text("Include ACTION ITEMS.")

    text, policy, versions = P.compose("persona", "report", directory=tmp_path)
    assert "Be terse." in text and "ACTION ITEMS" in text
    assert policy == {"max_words": 120}
    first = versions["persona"]

    # a non-developer edits the file; the next composition must reflect it, no restart
    (tmp_path / "persona.md").write_text("---\nmax_words: 500\n---\nBe expansive.")
    text2, policy2, versions2 = P.compose("persona", "report", directory=tmp_path)
    assert "Be expansive." in text2 and policy2 == {"max_words": 500}
    assert versions2["persona"] != first          # version hash changes, so traces attribute correctly


def test_missing_prompt_layer_does_not_crash(tmp_path):
    from analyst import prompts as P

    text, policy, versions = P.compose("nope", directory=tmp_path)
    assert text == "" and policy == {} and versions["nope"] == "missing"
