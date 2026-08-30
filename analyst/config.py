"""Runtime settings. Everything tunable lives here; nothing else reads the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SOURCE_DATASET = "bigquery-public-data.thelook_ecommerce"
SAFE_DATASET_NAME = "thelook_safe"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # credentials / project
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY") or "")
    project: str = field(default_factory=lambda: _env("GOOGLE_CLOUD_PROJECT") or "")
    openrouter_api_key: str | None = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))

    # One model for everything — the agent and every in-tool call — and one fallback behind it.
    # The task requires resilience to third-party downtime, and a single provider does not give that:
    # the Gemini key rate-limited at 20 requests/day during testing, which is exactly the failure the
    # fallback exists for. Gemini also supplies embeddings regardless.
    model: str = _env("MODEL", "google/gemini-2.5-flash")               # via OpenRouter
    fallback_model: str = _env("FALLBACK_MODEL", "gemini-3.5-flash")    # via Gemini direct
    embedding_model: str = "gemini-embedding-001"

    # Sampling temperature for every model call — the agent, the SQL writer, the report writer, the
    # memory reconciler and the judges. Zero throughout: the same question should give the same SQL
    # and the same verdict, or none of the evaluation numbers mean anything run to run.
    temperature: float = float(_env("TEMPERATURE", "0.0"))

    # BigQuery safety
    max_bytes_billed: int = 200 * 1024 * 1024          # 200 MB per query; the public tables are far smaller
    default_limit: int = 200                             # injected when the model forgets a LIMIT
    max_limit: int = 1000                                # hard clamp
    rows_to_model: int = 40                              # rows of a result the writer is allowed to see
    use_safe_views: bool = True                          # False => inline projection of users instead of the view

    # Similarity above which a trio's SQL may be replayed verbatim. Measured on 36 paraphrases and
    # 8 unrelated questions with gemini-embedding-001: paraphrases scored 0.63 and up, unrelated
    # questions 0.55 and below. Re-measure if the embedding model changes.
    tau_replay: float = 0.72

    # agent loop
    tool_call_limit: int = 10       # bounds the whole turn: goldens + data + report + library calls
    history_runs: int = 5           # previous turns the agent sees (follow-ups come from here)

    # in-tool budgets — these are spent inside a single tool call and never reach the agent's context
    max_sql_attempts: int = 3       # first try + 2 corrections, each with the previous error attached
    max_report_attempts: int = 2    # first draft + 1 retry if the post-hook rejects it

    # resilience
    model_timeout_s: int = 60

    # paths
    data_dir: Path = ROOT / "data"
    golden_dir: Path = ROOT / "golden"
    prompts_dir: Path = ROOT / "prompts"
    traces_dir: Path = ROOT / "data" / "traces"

    # offline judge — two families on purpose: same-family judges share failure modes and agree
    # confidently on the same wrong answer, which reads as corroboration but is not.
    judge_a: str = _env("JUDGE_A", "google/gemini-2.5-flash")
    judge_b: str = _env("JUDGE_B", "anthropic/claude-haiku-4.5")
    judge_idle_minutes: int = int(_env("JUDGE_IDLE_MINUTES", "20"))

    def judge_models(self) -> list[tuple[str, str]]:
        if not self.openrouter_api_key:
            return [("judge_a", self.fallback_model)]    # single judge; disagreement unavailable
        return [("judge_a", self.judge_a), ("judge_b", self.judge_b)]

    @staticmethod
    def today() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%A %d %B %Y")

    def provider_chain(self) -> list[tuple[str, str]]:
        """Providers to try, in order, for an in-tool completion. OpenRouter first when configured."""
        chain: list[tuple[str, str]] = []
        if self.openrouter_api_key:
            chain.append(("openrouter", self.model))
        if self.gemini_api_key:
            chain.append(("gemini", self.fallback_model))
        return chain

    @property
    def safe_dataset(self) -> str:
        return f"{self.project}.{SAFE_DATASET_NAME}"

    @property
    def db_path(self) -> Path:
        """Our own tables: reports, audit, learned, candidates, turns, judgements."""
        return self.data_dir / "analyst.db"

    @property
    def agno_db_path(self) -> Path:
        """Agno's sessions / runs / memory — kept in its own file so schemas never collide."""
        return self.data_dir / "agno.db"

    @property
    def lancedb_path(self) -> Path:
        return self.data_dir / "lancedb"

    def validate(self) -> list[str]:
        """Return human-readable problems instead of raising, so the CLI can show all of them at once."""
        problems = []
        if not self.gemini_api_key:
            problems.append("GEMINI_API_KEY is not set (see .env.example)")
        if not self.project:
            problems.append("GOOGLE_CLOUD_PROJECT is not set (see .env.example)")
        return problems


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.traces_dir.mkdir(parents=True, exist_ok=True)
