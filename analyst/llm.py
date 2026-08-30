"""Direct LLM calls for the work that happens *inside* tools.

The agent itself runs on Agno. The tools do not: each one makes a plain, single-purpose call with its
own prompt, so a failed SQL attempt or a rejected report draft never enters the agent's context.

OpenRouter is the primary provider (native REST, OpenAI-compatible); Gemini is the fallback so the
system still runs on a key-only setup. Both are reached through one `complete()` so the tools do not
know or care which answered.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from analyst.config import settings

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMUnavailable(RuntimeError):
    """Every configured provider failed. Callers must degrade, never present this as content."""


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1

    def usage(self) -> dict[str, Any]:
        return {"model": self.model, "provider": self.provider,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


def _openrouter(messages: list[Message], model: str, temperature: float, json_mode: bool, timeout: int) -> Completion:
    body: dict[str, Any] = {
        "model": model,
        "messages": [m.as_dict() for m in messages],
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        # OpenRouter asks for these; they identify the app on your dashboard.
        "HTTP-Referer": "https://github.com/thelook-analyst",
        "X-Title": "TheLook Analyst",
    }
    r = httpx.post(OPENROUTER_URL, json=body, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"openrouter {r.status_code}: {r.text[:200]}")
    data = r.json()
    usage = data.get("usage") or {}
    return Completion(
        text=(data["choices"][0]["message"]["content"] or "").strip(),
        model=data.get("model", model),
        provider="openrouter",
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )


def _gemini(messages: list[Message], model: str, temperature: float, json_mode: bool, timeout: int) -> Completion:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    system = "\n\n".join(m.content for m in messages if m.role == "system")
    convo = [m.content for m in messages if m.role != "system"]
    cfg = types.GenerateContentConfig(
        temperature=temperature,
        system_instruction=system or None,
        response_mime_type="application/json" if json_mode else None,
        http_options=types.HttpOptions(timeout=timeout * 1000),
    )
    res = client.models.generate_content(model=model, contents="\n\n".join(convo), config=cfg)
    meta = getattr(res, "usage_metadata", None)
    return Completion(
        text=(res.text or "").strip(),
        model=model,
        provider="gemini",
        input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
    )


def complete(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float | None = None,
    json_mode: bool = False,
    history: list[Message] | None = None,
) -> Completion:
    """One completion, trying each configured provider in turn.

    `model` overrides the configured one for callers that must pin a specific model — the judges,
    which need two named families. Everything else leaves it unset.
    """
    temperature = settings.temperature if temperature is None else temperature
    messages = [Message("system", system), *(history or []), Message("user", user)]
    plan = [("openrouter", model)] if model and settings.openrouter_api_key else settings.provider_chain()
    if not plan:
        raise LLMUnavailable("no provider configured — set OPENROUTER_API_KEY or GEMINI_API_KEY")

    last: Exception | None = None
    attempts = 0
    for provider, model in plan:
        for attempt in range(2):
            attempts += 1
            try:
                fn = _openrouter if provider == "openrouter" else _gemini
                out = fn(messages, model, temperature, json_mode, settings.model_timeout_s)
                if not out.text:
                    raise RuntimeError("empty completion")
                out.attempts = attempts
                return out
            except Exception as e:  # noqa: BLE001 - any provider failure moves us along the chain
                last = e
                msg = str(e)[:160]
                transient = any(s in msg for s in ("429", "500", "502", "503", "504", "timeout", "Timeout"))
                log.warning("%s/%s failed (%s): %s", provider, model, "transient" if transient else "hard", msg)
                if transient and attempt == 0:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    raise LLMUnavailable(f"all providers failed; last error: {str(last)[:200]}")


def complete_json(*, system: str, user: str, model: str | None = None, temperature: float | None = None,
                  history: list[Message] | None = None) -> tuple[dict, Completion]:
    """Completion that must return a JSON object. Retries once with the parse error attached."""
    out = complete(system=system, user=user, model=model, temperature=temperature, json_mode=True, history=history)
    try:
        return json.loads(_strip_fence(out.text)), out
    except json.JSONDecodeError as e:
        retry = complete(
            system=system,
            user=f"{user}\n\nYour previous reply was not valid JSON ({e}). Reply with a single JSON object and nothing else.",
            model=model, temperature=temperature, json_mode=True, history=history,
        )
        try:
            return json.loads(_strip_fence(retry.text)), retry
        except json.JSONDecodeError as e2:
            raise LLMUnavailable(f"model did not return JSON: {e2}") from e2


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    return t.strip()
