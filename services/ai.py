"""Provider-agnostic JSON-returning LLM client.

Every AI call in the bot goes through :func:`complete_json`. The provider wire
formats live behind that one function so that if Google ever changes its free
tier we can point the bot at any OpenAI-compatible endpoint (Groq, OpenRouter,
a local llama.cpp server, ...) by flipping ``AI_PROVIDER`` and editing only this
file.

The contract is deliberately narrow: give it a prompt and a JSON schema, get a
``dict`` back or an :class:`AIError`. Callers never see HTTP details, never see
provider-specific envelopes, and never have to defend against the model
wrapping its answer in a ```json fence.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from config import CFG, ConfigError
from services.http import HttpError, post

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MAX_OUTPUT_TOKENS = 2048
RETRY_BACKOFF_SECONDS = 2.0

# Keys Gemini's `responseSchema` understands. Anything else (notably
# `additionalProperties` and `$schema`, which json-schema tooling adds by
# reflex) makes the API reject the whole request with a 400.
_GEMINI_SCHEMA_KEYS = frozenset(
    {"type", "properties", "items", "required", "enum", "description"}
)

_GEMINI_TYPES = {
    "string": "STRING",
    "array": "ARRAY",
    "object": "OBJECT",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
}


class AIError(RuntimeError):
    """Any failure to obtain a valid JSON object from the model."""


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model reply.

    Tolerates a ```json fenced block and leading/trailing prose by locating the
    outermost ``{...}`` span. Raises ValueError when nothing parses, so callers
    can treat it as a retryable shape failure.
    """
    if not text or not text.strip():
        raise ValueError("model returned an empty response")

    candidate = text.strip()

    # Strip a fenced block if one is present; the fence may be ```json or bare.
    if "```" in candidate:
        fence_start = candidate.find("```")
        rest = candidate[fence_start + 3 :]
        newline = rest.find("\n")
        if newline != -1 and rest[:newline].strip().lower() in {"", "json"}:
            rest = rest[newline + 1 :]
        fence_end = rest.find("```")
        if fence_end != -1:
            rest = rest[:fence_end]
        candidate = rest.strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in model response")

    span = candidate[start : end + 1]
    try:
        parsed = json.loads(span)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def _gemini_schema(schema: Any) -> Any:
    """Rewrite a JSON Schema into the subset Gemini's responseSchema accepts."""
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "type":
            if isinstance(value, list):  # e.g. ["string", "null"] -> first concrete type
                value = next((v for v in value if v != "null"), "string")
            if isinstance(value, str):
                cleaned[key] = _GEMINI_TYPES.get(value.lower(), value.upper())
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: _gemini_schema(sub) for name, sub in value.items()}
            continue
        if key == "items":
            cleaned[key] = _gemini_schema(value)
            continue
        cleaned[key] = value
    return cleaned


def _gemini_blocked_reason(payload: dict) -> str | None:
    """Return a human message when Gemini refused or truncated, else None."""
    feedback = payload.get("promptFeedback") or {}
    block_reason = feedback.get("blockReason")
    if block_reason:
        return f"prompt blocked by Gemini safety filters (blockReason={block_reason})"

    candidates = payload.get("candidates") or []
    if not candidates:
        return "Gemini returned no candidates"

    finish = (candidates[0] or {}).get("finishReason")
    if finish == "SAFETY":
        return "Gemini stopped generation for safety reasons (finishReason=SAFETY)"
    if finish == "MAX_TOKENS":
        return (
            "Gemini hit the output token limit before finishing the JSON "
            f"(maxOutputTokens={MAX_OUTPUT_TOKENS})"
        )
    if finish == "RECITATION":
        return "Gemini stopped generation for recitation reasons"
    return None


def _gemini_text(payload: dict) -> str:
    """Extract candidates[0].content.parts[0].text, raising on any missing hop."""
    blocked = _gemini_blocked_reason(payload)
    if blocked:
        raise AIError(blocked)

    candidate = (payload.get("candidates") or [{}])[0] or {}
    parts = ((candidate.get("content") or {}).get("parts")) or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
    if not texts:
        raise AIError("Gemini response contained no text part")
    return "".join(texts)


def _call_gemini(prompt: str, schema: dict, temperature: float) -> dict:
    key = CFG.gemini_api_key
    if not key:
        raise ConfigError(
            "Missing required configuration: GEMINI_API_KEY. Set it as a GitHub "
            "Actions Secret (or in a local .env file)."
        )

    url = GEMINI_ENDPOINT.format(model=CFG.gemini_model)
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(schema),
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        },
    }
    # The key goes in a header, never the query string: URLs end up in logs,
    # exception messages and proxy access logs.
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}

    response = post(url, json=body, headers=headers)
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIError(f"Gemini returned a non-JSON envelope: {exc}") from exc

    return _extract_json(_gemini_text(payload))


# ---------------------------------------------------------------------------
# OpenAI-compatible (Groq, OpenRouter, local servers, ...)
# ---------------------------------------------------------------------------
_SCHEMA_INSTRUCTION = """

---
Return ONLY a single JSON object. No prose, no markdown fences, no commentary.
It must conform exactly to this JSON schema:
{schema}
"""


def _call_openai_compatible(prompt: str, schema: dict, temperature: float) -> dict:
    if not CFG.ai_base_url or not CFG.ai_api_key or not CFG.ai_model:
        raise ConfigError(
            "Missing required configuration: AI_BASE_URL, AI_API_KEY, AI_MODEL. "
            "These are required when AI_PROVIDER=openai_compatible."
        )

    url = CFG.ai_base_url.rstrip("/") + "/chat/completions"
    # Not every OpenAI-compatible server honours a strict schema, so the schema
    # is stated in the prompt and json_object mode is only a nudge.
    full_prompt = prompt + _SCHEMA_INSTRUCTION.format(
        schema=json.dumps(schema, ensure_ascii=False, indent=2)
    )
    body = {
        "model": CFG.ai_model,
        "messages": [{"role": "user", "content": full_prompt}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {CFG.ai_api_key}",
        "Content-Type": "application/json",
    }

    response = post(url, json=body, headers=headers)
    try:
        payload = response.json()
    except ValueError as exc:
        raise AIError(f"{CFG.ai_provider} returned a non-JSON envelope: {exc}") from exc

    choices = payload.get("choices") or []
    if not choices:
        error = payload.get("error")
        detail = f": {error}" if error else ""
        raise AIError(f"model returned no choices{detail}")

    message = (choices[0] or {}).get("message") or {}
    content = message.get("content") or ""
    if not content.strip():
        finish = (choices[0] or {}).get("finish_reason")
        raise AIError(f"model returned empty content (finish_reason={finish})")
    return _extract_json(content)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
_CORRECTION = (
    "\n\nIMPORTANT CORRECTION — your previous answer was rejected because: {problem}\n"
    "Reply again with ONLY a single valid JSON object matching the required "
    "schema. No markdown fences, no explanation, no trailing text."
)


def _model_name() -> str:
    return CFG.gemini_model if CFG.ai_provider == "gemini" else (CFG.ai_model or "unknown")


def complete_json(
    prompt: str,
    schema: dict,
    *,
    temperature: float = 0.4,
    max_retries: int = 2,
) -> dict:
    """Ask the configured provider for a JSON object matching `schema`.

    Retries both transport failures and unparseable/invalid JSON up to
    `max_retries` extra attempts. When the previous failure was about the shape
    of the answer, the retry prompt says so explicitly — models correct
    themselves far more reliably when told what was wrong.

    Raises:
        AIError: every attempt failed.
        ConfigError: the selected provider is missing its credentials.
    """
    provider = (CFG.ai_provider or "gemini").strip().lower()
    if provider == "gemini":
        caller = _call_gemini
    elif provider == "openai_compatible":
        caller = _call_openai_compatible
    else:
        raise AIError(
            f"Unknown AI_PROVIDER {provider!r}; expected 'gemini' or 'openai_compatible'"
        )

    model = _model_name()
    attempts = max(1, max_retries + 1)
    last_error = ""
    correction = ""  # non-empty only after a JSON-shape failure

    for attempt in range(1, attempts + 1):
        attempt_prompt = prompt + correction
        started = time.monotonic()
        try:
            result = caller(attempt_prompt, schema, temperature)
        except HttpError as exc:
            # A network blip says nothing about the prompt: retry it untouched.
            last_error, correction = f"transport failure: {exc}", ""
        except (ValueError, AIError) as exc:
            # Unparseable, empty, truncated or wrong-shaped output: tell the
            # model exactly what was wrong on the next go.
            last_error = str(exc)
            correction = _CORRECTION.format(problem=last_error)
        else:
            logger.info(
                "ai ok provider=%s model=%s attempt=%d/%d latency=%.2fs",
                provider,
                model,
                attempt,
                attempts,
                time.monotonic() - started,
            )
            return result

        logger.warning(
            "ai failed provider=%s model=%s attempt=%d/%d latency=%.2fs: %s",
            provider,
            model,
            attempt,
            attempts,
            time.monotonic() - started,
            last_error,
        )
        if attempt < attempts:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise AIError(
        f"AI call failed after {attempts} attempt(s) "
        f"(provider={provider}, model={model}): {last_error}"
    )
