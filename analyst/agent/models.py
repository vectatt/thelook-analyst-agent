"""Models for the Agno agent itself.

The tools reach providers through `analyst/llm.py`; this module is only about the agent that calls
them. Both use OpenRouter first and fall back to Gemini direct.

No thinking flags are set: Gemini 3.x rejects `thinking_budget=0` with HTTP 400.
"""

from __future__ import annotations

import logging

from agno.models.google import Gemini

from analyst.config import settings

log = logging.getLogger(__name__)


def _chain(temperature: float | None = None) -> list:
    """Every model the agent may fall back through, best first."""
    temperature = settings.temperature if temperature is None else temperature
    out: list = []
    if settings.openrouter_api_key:
        try:
            from agno.models.openrouter import OpenRouter
            out.append(OpenRouter(id=settings.model, api_key=settings.openrouter_api_key,
                                  temperature=temperature))
        except ImportError:  # pragma: no cover
            log.warning("OpenRouter configured but the `openai` package is missing")
    if settings.gemini_api_key:
        out.append(Gemini(id=settings.fallback_model, api_key=settings.gemini_api_key,
                          temperature=temperature, timeout=settings.model_timeout_s))
    return out


def primary_model(*, temperature: float | None = None):
    chain = _chain(temperature)
    if not chain:
        raise RuntimeError("no model configured — set OPENROUTER_API_KEY or GEMINI_API_KEY")
    return chain[0]


def fallback_models(*, temperature: float | None = None) -> list:
    return _chain(temperature)[1:]


def describe_chain() -> str:
    """One line naming the models in play. No square brackets — the CLI renders this through rich,
    which would read them as markup tags and silently drop the contents."""
    def name(m) -> str:
        return f"{type(m).__name__.lower()}:{getattr(m, 'id', '?')}"
    agent = " → ".join(name(m) for m in _chain())
    tools = " → ".join(f"{p}:{m}" for p, m in settings.provider_chain())
    return f"agent {agent} · tools {tools}"


class ModelUnavailable(RuntimeError):
    """Every model in the chain failed; the caller must degrade, never present the error as content."""


_GUARDRAIL_MARKERS = ("Potential PII detected", "prompt injection detected", "jailbreaking", "Validation failed")


def run_failed(run) -> str | None:
    """Agno does not raise when retries and fallbacks are exhausted — it returns a RunOutput whose
    status is an error and whose content is the provider's error text. Detect that here so no stage
    ever treats an error payload as an answer."""
    status = getattr(run, "status", None)
    name = str(getattr(status, "value", status) or "").lower()
    content = run.content if isinstance(run.content, str) else ""
    if guardrail_fired(run):
        return None
    if name == "error":
        return content[:300] or "model error"
    stripped = content.lstrip()
    if stripped.startswith("{") and '"error"' in stripped[:200]:
        return content[:300]
    return None


def guardrail_fired(run) -> bool:
    content = run.content if isinstance(run.content, str) else ""
    return any(m.lower() in content.lower() for m in _GUARDRAIL_MARKERS)
