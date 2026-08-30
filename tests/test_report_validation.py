"""The report post-hook — pure, no network.

This is the check that stops a report asserting a number nobody queried. It fires before the manager
sees the draft, and a failure sends the draft back once with the problem named.
"""

import pytest

from analyst.tools.reporting import _ungrounded_figures, validate

DATA = """3 row(s), 9.6 MB scanned:
| state          | customers | revenue | revenue_per_customer |
| South Carolina |       213 |   27016 |               126.84 |
| Tennessee      |       247 |   27850 |               112.75 |"""

GOOD = """# Tennessee vs South Carolina
Tennessee customers spend $112.75 each against South Carolina's $126.84.

## Findings
- Tennessee has 247 customers to South Carolina's 213.
- Revenue is close ($27,850 vs $27,016) but spread over more people.

## Why
Lower spend per head, not fewer buyers.

## ACTION ITEMS
1. Test premium merchandising in Tennessee by the end of Q3 with the regional team.
"""


def test_a_grounded_report_passes():
    assert validate(GOOD, DATA, want_actions=True) == []


def test_invented_figures_are_caught():
    bad = GOOD.replace("$112.75", "$998.10").replace("247 customers", "9,412 customers")
    problems = validate(bad, DATA, want_actions=True)
    assert any("do not appear in the data" in p for p in problems)


def test_rounded_figures_are_accepted():
    """The persona tells the writer to round money to whole dollars, so a faithful report says
    $7,852 for a queried 7851.78. A string comparison called that a fabrication — it is not."""
    data = "| brand | revenue |\n| Calvin Klein | 7851.78 |\n| True Religion | 9295.80 |\n| total | 23305.68 |"
    assert _ungrounded_figures(
        "Calvin Klein reached $7,852 and True Religion $9,296, totalling $23,306.", data) == []
    assert _ungrounded_figures("Revenue was $27,016 across 213 customers.", DATA) == []


def test_fabrication_is_still_caught_after_the_rounding_tolerance():
    data = "| brand | revenue |\n| Calvin Klein | 7851.78 |"
    missing = _ungrounded_figures("Revenue was $998,120 across 4,412 customers.", data)
    assert set(missing) == {"$998,120", "4,412"}


def test_missing_required_section_is_caught():
    problems = validate(GOOD.replace("## ACTION ITEMS", "## Notes"), DATA, want_actions=True)
    assert any("action items" in p.lower() for p in problems)


def test_persona_word_limit_is_enforced():
    """A non-developer sets `max_words` in persona.md and the post-hook makes it real."""
    long_report = GOOD + "\n" + ("filler word " * 300)
    problems = validate(long_report, DATA, want_actions=True, policy={"max_words": 120})
    assert any("over the 120-word limit" in p for p in problems)
    assert validate(GOOD, DATA, want_actions=True, policy={"max_words": 400}) == []


def test_custom_required_sections_come_from_policy():
    problems = validate(GOOD, DATA, want_actions=True, policy={"require_sections": ["risks"]})
    assert any("'risks' section is missing" in p for p in problems)


@pytest.mark.parametrize("report", ["", "   ", "Too short."])
def test_empty_or_tiny_reports_are_rejected(report):
    assert validate(report, DATA, want_actions=False) != []
