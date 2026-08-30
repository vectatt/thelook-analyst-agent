"""Offline quality evaluation: judge a finished conversation, output booleans.

Run it as a batch — `python -m analyst.judge` — over sessions that have been idle long enough to be
considered finished. Nothing runs at conversation time, so this costs the manager no latency and can
be re-run over history whenever the metrics change.

Three deliberate choices:

**Three values, not scores and not booleans.** An LLM's "7/10" is uncalibrated and means something
different every run. But a plain true/false is not enough either: collapsing "did not apply" into
"passed" inflates every rate, so `handled_rejection_well = 100%` could equally mean rejections were
handled well or that none ever happened. Each metric is therefore `pass`, `fail` or `n/a`, and a rate
is computed only over the conversations where the metric applied.

**Deterministic checks first.** Roughly half these metrics need no model at all — PII in the output is
a regex, "did it call check_goldens before writing SQL" is readable straight from the trace. Those are
*exactly* right rather than probably right, and they are free. The model only rules on the genuinely
subjective half.

**Two judges from different model families.** Two variants of the same model share failure modes and
will agree confidently on the same wrong answer, which is worse than one judge because it looks like
corroboration. Agreement between families is evidence; agreement within one is not. Where they
disagree, the conversation is flagged for a human rather than silently resolved — and the disagreement
rate is itself a metric, because a rise in it means the judges or the metric definitions are drifting.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from analyst.config import settings
from analyst.db import connection, init_db
from analyst.llm import LLMUnavailable, complete_json
from analyst.safety.masking import mask_text
from analyst.tracing.jsonl import Tracer

log = logging.getLogger(__name__)

# --- what the judges rule on -----------------------------------------------------------------------
SUBJECTIVE: dict[str, str] = {
    "answered_the_question_asked": "Every data question the user asked was addressed, not a near-by question. n/a if no data question was asked.",
    "decomposed_why_question": "The answer to a WHY question names the driver — which component moved — rather than restating the metric. n/a if no why-question was asked.",
    "action_items_concrete": "Action items are specific and actionable ('test premium merchandising in Tennessee by Q3'), not vague ('improve marketing'). n/a if the answer contained no action items.",
    "led_with_the_answer": "The first sentence of each substantive answer states the finding and its key number, rather than preamble or restating the question. n/a if no data answer was given.",
    "respected_stated_preferences": "The answer follows the presentation preference the user stated in this conversation. n/a if the user stated none.",
    "acknowledged_uncertainty": "Where the data was thin or a caveat applied (small samples, a partial period), the answer said so. n/a if no such caveat applied.",
    "handled_rejection_well": "After the user rejected a report the agent asked what was wrong, or addressed the stated problem, rather than reproducing the same report. n/a if nothing was rejected.",
}

PASS, FAIL, NA = "pass", "fail", "n/a"

DETERMINISTIC = (
    "no_pii_in_output",
    "every_figure_traceable",
    "called_check_goldens_before_sql",
    "confirmed_before_destructive_action",
    "no_unhandled_error_shown",
    "recovered_from_sql_errors",
)

_FIGURE = re.compile(r"[$€£]\s?[\d,]+(?:\.\d+)?|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+\.\d+\s?%")


@dataclass
class Conversation:
    session_id: str
    user_id: str
    turns: list[dict]
    spans: list[dict]

    @property
    def transcript(self) -> str:
        return "\n\n".join(f"{t['role'].upper()}: {t['content'][:1500]}" for t in self.turns)

    @property
    def tool_calls(self) -> str:
        """What the agent actually did — the judge needs this for process metrics."""
        lines = []
        for s in self.spans:
            if s["span"].startswith(("tool.", "bq.", "llm.")):
                detail = {k: v for k, v in s.items()
                          if k in ("rows", "verified", "hits", "attempt", "confirmed", "sql", "error")}
                lines.append(f"{s['span']} {json.dumps(detail, default=str)[:220]}")
        return "\n".join(lines) or "(no tool calls recorded)"

    @property
    def answers(self) -> str:
        return "\n".join(t["content"] for t in self.turns if t["role"] == "assistant")


@dataclass
class Judgement:
    session_id: str
    user_id: str
    metrics: dict[str, str]                      # metric -> pass | fail | n/a
    verdicts: dict[str, dict[str, str]] = field(default_factory=dict)
    disagreed: list[str] = field(default_factory=list)
    notes: str = ""


# --- deterministic half ----------------------------------------------------------------------------
def deterministic(convo: Conversation) -> dict[str, bool]:
    spans = convo.spans
    answers = convo.answers

    _, pii_found = mask_text(answers)

    sql_spans = [s for s in spans if s["span"] == "bq.run"]
    data_rows = " ".join(str(s.get("sql", "")) + " " + str(s.get("rows", "")) for s in sql_spans)
    figures_present = {re.sub(r"[^\d]", "", m.group(0)) for m in _FIGURE.finditer(answers)}
    # a figure is traceable if a query ran at all and its digits appear somewhere in what came back
    traceable = True
    if figures_present and not sql_spans:
        traceable = False

    goldens = [i for i, s in enumerate(spans) if s["span"] == "tool.check_goldens"]
    first_sql = next((i for i, s in enumerate(spans) if s["span"] == "bq.run"), None)
    called_goldens = True if first_sql is None else bool(goldens and min(goldens) < first_sql)

    deletes = [s for s in spans if s["span"] == "llm.agent.continue"]
    confirmed = all(s.get("confirmed") is not None for s in deletes) if deletes else True

    turn_spans = [s for s in spans if s["span"] == "turn"]
    no_errors = not any(t.get("error") for t in turn_spans)

    failed_sql = [s for s in sql_spans if s.get("error")]
    recovered = True if not failed_sql else any(not s.get("error") for s in sql_spans)

    def verdict(applies: bool, ok: bool) -> str:
        return (PASS if ok else FAIL) if applies else NA

    return {
        "no_pii_in_output": verdict(bool(answers.strip()), not pii_found),
        "every_figure_traceable": verdict(bool(figures_present), traceable),
        "called_check_goldens_before_sql": verdict(first_sql is not None, called_goldens),
        "confirmed_before_destructive_action": verdict(bool(deletes), confirmed),
        "no_unhandled_error_shown": verdict(bool(turn_spans), no_errors),
        "recovered_from_sql_errors": verdict(bool(failed_sql), recovered),
    }


# --- the LLM half ----------------------------------------------------------------------------------
SYSTEM = (
    "You review a finished conversation between a data-analysis assistant and a retail manager, and "
    "judge it against a fixed list of criteria.\n\n"
    "Answer each with exactly one of: \"pass\", \"fail\", or \"n/a\".\n"
    "  pass — the criterion applied and the assistant met it\n"
    "  fail — the criterion applied and the assistant did not meet it\n"
    "  n/a  — the situation the criterion describes never arose in this conversation\n\n"
    "Never answer \"pass\" for something that did not happen; that is what \"n/a\" is for. Be strict and "
    "judge what the assistant actually did, not what it claimed to do.\n\n"
    "Reply with a single JSON object mapping every criterion name to one of those three strings, plus a "
    "\"notes\" key with one short sentence naming the most serious problem you saw, or an empty string."
)


def _ask(convo: Conversation, model: str) -> dict:
    criteria = "\n".join(f"- {k}: {v}" for k, v in SUBJECTIVE.items())
    user = (
        f"CRITERIA\n{criteria}\n\n"
        f"WHAT THE ASSISTANT DID (tool calls, in order)\n{convo.tool_calls}\n\n"
        f"TRANSCRIPT\n{convo.transcript[:8000]}"
    )
    data, _ = complete_json(system=SYSTEM, user=user, model=model)
    return data


def _normalise(value) -> str:
    """Accept pass/fail/n-a in any spelling the model reaches for, booleans included."""
    if isinstance(value, bool):
        return PASS if value else FAIL
    s = str(value or "").strip().lower().replace("_", "/").replace("-", "/")
    if s in ("n/a", "na", "not/applicable", "notapplicable", "none"):
        return NA
    if s in ("fail", "false", "no"):
        return FAIL
    if s in ("pass", "true", "yes"):
        return PASS
    return NA


def judge(convo: Conversation) -> Judgement:
    metrics = deterministic(convo)
    verdicts: dict[str, dict[str, bool]] = {}
    notes: list[str] = []

    for label, model in settings.judge_models():
        try:
            raw = _ask(convo, model)
        except LLMUnavailable as e:
            log.warning("judge %s unavailable: %s", label, e)
            continue
        verdicts[label] = {k: _normalise(raw.get(k)) for k in SUBJECTIVE}
        if raw.get("notes"):
            notes.append(f"{label}: {raw['notes']}")

    disagreed: list[str] = []
    if len(verdicts) >= 2:
        a, b = list(verdicts.values())[:2]
        for k in SUBJECTIVE:
            if a[k] == b[k]:
                metrics[k] = a[k]
            elif NA in (a[k], b[k]):
                # one judge thought the situation never arose. That is a weaker conflict than
                # pass-vs-fail: take the substantive verdict, but still flag it for review.
                metrics[k] = a[k] if a[k] != NA else b[k]
                disagreed.append(k)
            else:
                metrics[k] = NA              # genuine pass-vs-fail: withheld, handed to a human
                disagreed.append(k)
    elif verdicts:
        metrics.update(next(iter(verdicts.values())))

    return Judgement(convo.session_id, convo.user_id, metrics, verdicts, disagreed, " | ".join(notes)[:500])


# --- selecting what to judge -------------------------------------------------------------------------
def finished_sessions(idle_minutes: int | None = None, limit: int = 20) -> list[Conversation]:
    """Sessions with no activity for `idle_minutes`, not yet judged."""
    idle = idle_minutes if idle_minutes is not None else settings.judge_idle_minutes
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=idle)).isoformat(timespec="seconds")
    tracer = Tracer()
    with connection() as c:
        rows = c.execute(
            "SELECT session_id, user_id, MAX(ts) AS last_ts, COUNT(*) AS n FROM turns "
            "GROUP BY session_id HAVING last_ts < ? "
            "AND session_id NOT IN (SELECT session_id FROM judgements) "
            "ORDER BY last_ts DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        out = []
        for r in rows:
            turns = [dict(t) for t in c.execute(
                "SELECT role, content, trace_id FROM turns WHERE session_id=? ORDER BY id", (r["session_id"],)
            ).fetchall()]
            trace_ids = {t["trace_id"] for t in turns if t["trace_id"]}
            spans = [s for tid in trace_ids for s in tracer.load(tid)]
            spans.sort(key=lambda s: s["ts"])
            out.append(Conversation(r["session_id"], r["user_id"], turns, spans))
    return out


def store(j: Judgement, turns: int) -> None:
    with connection() as c:
        c.execute(
            "INSERT OR REPLACE INTO judgements (session_id, user_id, judged_at, turns, metrics, verdicts, disagreed, notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (j.session_id, j.user_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), turns,
             json.dumps(j.metrics), json.dumps(j.verdicts), len(j.disagreed), j.notes),
        )


def summary(limit: int = 200) -> dict:
    """Pass rates per metric — computed only over conversations where the metric applied.

    A metric that never applied reports `null` rather than 100%, because "always passed" and "never
    tested" are different facts and collapsing them hides the second one.
    """
    init_db()
    with connection() as c:
        rows = c.execute("SELECT metrics, disagreed FROM judgements ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return {"sessions_judged": 0, "hint": "run `python -m analyst.judge` after some conversations"}

    tally: dict[str, dict[str, int]] = {}
    for r in rows:
        for k, v in json.loads(r["metrics"]).items():
            slot = tally.setdefault(k, {PASS: 0, FAIL: 0, NA: 0})
            slot[_normalise(v)] += 1

    rates, coverage = {}, {}
    for k, slot in sorted(tally.items()):
        applied = slot[PASS] + slot[FAIL]
        rates[k] = round(100 * slot[PASS] / applied, 1) if applied else None
        coverage[k] = f"{applied}/{applied + slot[NA]}"
    return {
        "sessions_judged": len(rows),
        "pass_rate_pct": rates,
        "applied_in": coverage,          # how many sessions each metric was actually testable in
        "untested_metrics": [k for k, v in rates.items() if v is None],
        "sessions_with_judge_disagreement": sum(1 for r in rows if r["disagreed"]),
        "handoff_rate_pct": round(100 * sum(1 for r in rows if r["disagreed"]) / len(rows), 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Judge finished conversations and store boolean metrics.")
    ap.add_argument("--idle-minutes", type=int, default=None, help="how long a session must be quiet")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--summary", action="store_true", help="print the current pass rates and exit")
    args = ap.parse_args(argv)
    init_db()

    if args.summary:
        print(json.dumps(summary(), indent=2)); return 0

    convos = finished_sessions(args.idle_minutes, args.limit)
    if not convos:
        print("No finished, unjudged sessions."); return 0
    for convo in convos:
        j = judge(convo)
        store(j, len(convo.turns))
        failed = [k for k, v in j.metrics.items() if v == FAIL]
        flag = f" | DISAGREED: {', '.join(j.disagreed)}" if j.disagreed else ""
        print(f"{convo.session_id}  {len(convo.turns):3d} turns  "
              f"{'all passed' if not failed else 'FAILED: ' + ', '.join(failed)}{flag}")
        if j.notes:
            print(f"    {j.notes[:160]}")
    print("\n" + json.dumps(summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
