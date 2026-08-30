# TheLook Analyst Agent

A CLI chat assistant that lets non-technical managers ask questions about sales, customers and products
in plain English over `bigquery-public-data.thelook_ecommerce`, discuss the answers, revise reports
until they are right, and save them.

Design and reasoning: **[docs/HLD.md](docs/HLD.md)**.

```
you › Why are customers in Tennessee underspending compared to South Carolina?
╭──────────────────────────────────────────────────────────────────────────────╮
│ Customers in Tennessee spend $113 per person, 11.1% less than the $127 spent │
│ in South Carolina. The gap is both a smaller basket (1.99 vs 2.10 items per  │
│ customer) and a lower average item price ($56.61 vs $60.30). ...             │
╰──────────────────────────────────────────────────────────────────────────────╯
✓ verified analysis · 1 query · /sql shows it, /trace shows the spans
```

## Architecture in one paragraph

**One agent. Every capability is a tool. The tools that need judgement make their own model call
inside themselves**, so failed SQL, rejected drafts and error text never enter the agent's context.
The agent decides what to do; the tools decide how. What is *not* left to the model: which tables the
credential can reach, whether a deletion executes, and whether a figure may appear in an answer.

```
user message
   │
   └─► agent  (session state · learned preferences · guardrails on input · ≤10 tool calls)
         ├─ check_goldens ......... what human analysts already worked out for this question
         ├─ get_info_from_db ...... LLM writes SQL → guard → dry-run → execute → PII mask → rows
         │                          on failure, called again WITH the error, so it can't repeat it
         ├─ generate_report ....... LLM drafts → post-hook: grounded? structured? within policy?
         │                          rejected → one retry naming the problem
         ├─ save_report ........... + description + queued as a golden candidate
         ├─ get_reports / show .... searchable by description
         ├─ delete_reports ⏸ ...... pauses for a typed DELETE
         └─ remember .............. dedups and resolves contradictions before writing
```

## What it does

| Capability | How |
|---|---|
| Answers any question over the four tables | the agent asks `get_info_from_db` in plain words; SQL is generated, guarded and corrected inside the tool |
| Reuses **human-verified analyses** | `check_goldens` returns the analyst's notes and SQL; a strong match is replayed byte-for-byte and labelled verified |
| **Multi-step** questions | the agent chains tool calls ("top 3 brands, then each one's trend") |
| **Reports you can argue with** | reject it → the agent asks what was wrong → regenerates addressing that; if the *data* was wrong it re-queries first |
| **Never quotes a number it didn't query** | post-hook checks every figure against the rows it was given |
| **Never exposes customer identities** | PII-free views + SQL guard + masking on every result set |
| **Deletes only after a typed confirmation** | preview of exactly what goes, soft delete, audit trail |
| **Learns how you like answers** | free-text observations, deduped and contradiction-checked on write |
| **Learns what the team should do** | approved reports are queued as candidate golden analyses for an analyst to promote |
| Change the tone without a deploy | edit `prompts/persona.md`; the next message uses it |
| Quality measured after the fact | offline judge → pass/fail/n-a metrics, two model families, disagreements handed off, calibratable against human labels |

## Setup (≈10 minutes)

Requirements: Python 3.12, a Google account, `gcloud`.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**1. Keys.** A Gemini key from https://aistudio.google.com/apikey, and an OpenRouter key from
https://openrouter.ai/keys (runs the agent, the in-tool calls and the two judges).

**2. BigQuery.** Any GCP project; the public dataset is free within 1 TB/month.

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable bigquery.googleapis.com
```

**3. Configure and verify.**

```bash
cp .env.example .env      # fill GEMINI_API_KEY, OPENROUTER_API_KEY, GOOGLE_CLOUD_PROJECT
bash verify_bq.sh         # expect "ALL GREEN"
```

**4. Create the PII-free views and index the verified analyses.**

```bash
python scripts/make_views.py    # <project>.thelook_safe.{users_safe,orders,order_items,products}
python scripts/seed_golden.py   # runs every trio against BigQuery, then builds the local index
```

`users_safe` has no name, e-mail, address, postal code or coordinates — that is what the agent's
credential is pointed at. If you skip this, the SQL guard falls back to an inline projection of the
safe columns, so the guarantee holds either way; the views are simply the stronger form.

**5. Run.**

```bash
python -m analyst.cli --user alice
```

## Using it

Ask in plain English; follow-ups work. `/help` lists commands.

| Command | |
|---|---|
| `/reports [text]` · `/show <id>` | the saved-report library |
| `/memory` · `/forget <id>` | what the agent has learned about you |
| `/candidates` · `/promote <id>` | approved analyses awaiting promotion to verified |
| `/sql` · `/trace [id]` · `/metrics` | what ran, and how the system is behaving |
| `/quality` | judge verdicts, pass rates, and which metrics were never tested |
| `/prompts` | the live prompt layers and their versions |

**Deleting** — just ask ("delete the reports about Nike"). You see exactly which reports match and
type `DELETE`. Anything else cancels. You only ever see your own.

## Changing behaviour without touching code

Five prompt layers in `prompts/`, each with a different owner, re-read on every message:

| File | Owner |
|---|---|
| `persona.md` | CEO / marketing — tone, and a policy block the report post-hook **enforces** |
| `report.md` | analyst — report structure |
| `conventions.md` | analyst — what revenue means, how to compare regions |
| `sql.md` | engineer — SQL rules |
| `agent.md` | engineer — which tools, when |

`persona.md` front-matter is enforced, not merely requested:

```yaml
---
max_words: 800
require_sections: [action items]
---
```

Each file is content-hashed and the hash goes into the trace, so any answer can be attributed to the
prompt version that produced it.

## Quality evaluation

```bash
python -m analyst.judge                    # judge conversations idle for 20+ minutes
python -m analyst.judge --summary          # pass rates
```

Each metric is **`pass` / `fail` / `n/a`** — not a score, and not a boolean. Scores from an LLM are
uncalibrated; booleans are worse than they look, because "did not apply" collapses into "passed" and a
metric reads 100% when it was never actually tested. Rates are computed only over the conversations
where a metric applied, and a metric that never applied reports `null` and appears under
`untested_metrics`.

Six metrics are decided **deterministically from the trace** — PII in the output is a regex,
`called_check_goldens_before_sql` is span ordering — so they are exactly right and free. Seven are
judged by **two models from different families** (`gemini-2.5-flash` and `claude-haiku-4.5`), because
same-family judges share failure modes and agree confidently on the same wrong answer. Where they
disagree the verdict is withheld and the conversation is flagged for a human.

**Treat the rates as directional.** The judged metrics are not validated against human judgement, so
they show trends, not truth, and no build should be gated on them until they are. Validating them means
labelling a set of conversations by hand and comparing per metric and per judge — agreement percentage
alongside Cohen's kappa, since a metric that is nearly always "pass" scores high agreement by chance.

## Tests

```bash
python -m pytest tests -q        # 74 unit tests, no network
python scripts/scenarios.py      # 26 behavioural assertions over 7 live scenarios
python scripts/smoke_agent.py    # one long conversation, printed for reading
```

`scenarios.py` is the pre-deployment check: independent sessions, each asserting a behaviour a person
would otherwise have to catch by reading transcripts — a follow-up runs its own query rather than
answering from the last result, one manager's preferences never reach another's library, an undefined
metric like churn is never given a value that was not queried, a paused deletion resumes correctly in a
different process. It found three real defects that the unit tests could not.

## Project layout

```
analyst/
  config.py             settings, models, budgets, thresholds
  llm.py                direct provider calls for in-tool work (OpenRouter → Gemini fallback)
  prompts.py            the five prompt layers: compose, hot-reload, policy, versioning
  schema.py             the four safe views and the PII deny-list
  memory.py             what the agent has learned; dedup + contradiction resolution
  safety/sql_guard.py   SELECT-only · allow-listed tables · PII deny · LIMIT · rewrite to safe views
  safety/masking.py     PII masking on result sets, before rows reach the agent
  bq/tool.py            guard → dry-run → byte cap → execute
  golden/               trio model · LanceDB index · candidate queue
  reports/library.py    owner-scoped, soft delete, audit trail
  tools/                get_info_from_db · generate_report · check_goldens · get_schema
  agent/                the agent, its toolset, one turn end to end
  judge.py              offline evaluation: pass/fail/n-a, deterministic + two-family LLM judges
  cli.py
prompts/                agent · persona · report · conventions · sql
golden/trios/           10 verified analyses
```

## Notes on models

**Temperature is 0 for every call** — the agent, the SQL writer, the report writer, the memory
reconciler and both judges — set once in `config.py` and overridable with `TEMPERATURE`. The same
question should produce the same SQL and the same verdict, or the quality numbers cannot be compared
between runs.

One model for everything — `google/gemini-2.5-flash` via OpenRouter — with Gemini direct behind it as
the fallback, because the task requires resilience to third-party downtime and a single provider does
not give that. Judges are `google/gemini-2.5-flash` and `anthropic/claude-haiku-4.5`, deliberately
different families: same-family judges share failure modes, so agreeing on the same wrong answer looks
like corroboration. All overridable in `.env`.
