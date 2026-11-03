"""
slide_gen/llm_client.py
-----------------------
Thin wrappers around the Anthropic and OpenAI SDKs.
Both clients expose the same interface so slide_tab.py doesn't need
to branch on provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Provider model lists
# ---------------------------------------------------------------------------

ANTHROPIC_MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-haiku-4-5"
]

OPENAI_MODELS = [
    "gpt-4o",
    "gpt-oss-120b",
    "gpt-4o-mini",
    "o1",
    "o4-mini",
]

MAX_INPUT_BYTES = 48000
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 2

# Price per million tokens (input, output) in USD
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "o1": (15.00, 60.00),
    "o4-mini": (1.10, 4.40),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (15.00, 75.00),
    "claude-opus-5": (15.00, 75.00),
    "claude-haiku-4-5": (0.80, 4.00),
}


@dataclass
class UsageReport:
    """Usage stats and status for a single LLM request."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    status: str = "success"


def log_request_usage(model: str, usage: UsageReport, log_dir: Path | str | None = None) -> None:
    """
    Append request usage record to .slide_gen_cache/usage_log.jsonl.
    Unknown tokens are recorded as 'unknown', not as zero.
    """
    try:
        cache_dir = Path(log_dir) if log_dir else Path(".slide_gen_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        log_file = cache_dir / "usage_log.jsonl"

        cost: float | str = "unknown"
        if (
            usage.input_tokens is not None
            and usage.output_tokens is not None
            and model in MODEL_PRICES
        ):
            in_p, out_p = MODEL_PRICES[model]
            cost = round((usage.input_tokens * in_p + usage.output_tokens * out_p) / 1_000_000, 6)

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "input_tokens": usage.input_tokens if usage.input_tokens is not None else "unknown",
            "output_tokens": usage.output_tokens if usage.output_tokens is not None else "unknown",
            "status": usage.status,
            "estimated_cost_usd": cost,
        }
        with open(log_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(record) + "\n")
    except (OSError, RuntimeError):
        pass


def check_request_budget(system: str, user: str) -> None:
    """Limit combined UTF-8 bytes as a conservative input-token proxy."""
    input_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
    if input_bytes > MAX_INPUT_BYTES:
        raise ValueError(
            f"Request input is {input_bytes} UTF-8 bytes; "
            f"the limit is {MAX_INPUT_BYTES} bytes."
        )


class IncompleteGenerationError(RuntimeError):
    """The response did not produce nonempty text with a successful stop."""


class FatalAPIError(RuntimeError):
    """Fatal provider error (e.g. bad API key, quota exceeded, permission denied)."""


def _is_fatal_error(exc: Exception) -> bool:
    name = exc.__class__.__name__
    msg = str(exc).lower()
    return (
        name in ("AuthenticationError", "PermissionDeniedError")
        or "invalid_api_key" in msg
        or "insufficient_quota" in msg
        or "billing" in msg
    )


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class LLMClient:
    """Interface all provider clients must implement."""

    last_usage: UsageReport | None = None

    def complete(self, system: str, user: str) -> str:
        """Return the full completion as a string (blocks until done)."""
        return "".join(self.stream(system, user))

    def stream(self, system: str, user: str) -> Iterator[str]:
        """Yield provisional text; completion requires exhausting the iterator."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

class AnthropicClient(LLMClient):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        try:
            import anthropic as _ant
        except ImportError:
            raise ImportError(
                "anthropic package not installed. "
                "Run: pip install anthropic"
            )
        self._client = _ant.Anthropic(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )
        self._model = model

    def stream(self, system: str, user: str) -> Iterator[str]:
        check_request_budget(system, user)
        has_text = False
        input_tokens = None
        output_tokens = None
        try:
            with self._client.messages.stream(
                model=self._model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                for text in stream.text_stream:
                    if text:
                        has_text = has_text or bool(text.strip())
                        yield text
                try:
                    message = stream.get_final_message()
                except RuntimeError as error:
                    raise IncompleteGenerationError(
                        "Anthropic stream ended without a final message."
                    ) from error

                usage = getattr(message, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "input_tokens", None)
                    output_tokens = getattr(usage, "output_tokens", None)

                stop_reason = getattr(message, "stop_reason", None)
                if stop_reason != "end_turn":
                    raise IncompleteGenerationError(
                        f"Anthropic generation did not complete: {stop_reason!r}."
                    )
                if not has_text:
                    raise IncompleteGenerationError(
                        "Anthropic generation returned empty text."
                    )
            self.last_usage = UsageReport(input_tokens=input_tokens, output_tokens=output_tokens, status="success")
            log_request_usage(self._model, self.last_usage)
        except Exception as exc:
            self.last_usage = UsageReport(input_tokens=input_tokens, output_tokens=output_tokens, status="error")
            log_request_usage(self._model, self.last_usage)
            if _is_fatal_error(exc):
                raise FatalAPIError(f"Anthropic fatal error: {exc}") from exc
            raise


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        try:
            import openai as _oai
        except ImportError:
            raise ImportError(
                "openai package not installed. "
                "Run: pip install openai"
            )
        self._client = _oai.OpenAI(
            api_key=api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=MAX_RETRIES,
        )
        self._model = model

    def stream(self, system: str, user: str) -> Iterator[str]:
        check_request_budget(system, user)
        has_text = False
        finish_reason = None
        input_tokens = None
        output_tokens = None
        try:
            with self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_completion_tokens=16000,
                stream=True,
                stream_options={"include_usage": True},
            ) as stream:
                for chunk in stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        input_tokens = getattr(chunk_usage, "prompt_tokens", None)
                        output_tokens = getattr(chunk_usage, "completion_tokens", None)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta and getattr(delta, "refusal", None) is not None:
                        raise IncompleteGenerationError(
                            "OpenAI generation was refused."
                        )
                    if delta and delta.content:
                        has_text = has_text or bool(delta.content.strip())
                        yield delta.content
                    reason = getattr(choice, "finish_reason", None)
                    if reason is not None:
                        if reason != "stop":
                            raise IncompleteGenerationError(
                                f"OpenAI generation did not complete: {reason!r}."
                            )
                        finish_reason = reason
                if finish_reason != "stop":
                    raise IncompleteGenerationError(
                        "OpenAI stream ended without a successful finish reason."
                    )
                if not has_text:
                    raise IncompleteGenerationError(
                        "OpenAI generation returned empty text."
                    )
            self.last_usage = UsageReport(input_tokens=input_tokens, output_tokens=output_tokens, status="success")
            log_request_usage(self._model, self.last_usage)
        except Exception as exc:
            self.last_usage = UsageReport(input_tokens=input_tokens, output_tokens=output_tokens, status="error")
            log_request_usage(self._model, self.last_usage)
            if _is_fatal_error(exc):
                raise FatalAPIError(f"OpenAI fatal error: {exc}") from exc
            raise


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_client(provider: str, api_key: str, model: str) -> LLMClient:
    """
    Return the right LLMClient for *provider*.

    Parameters
    ----------
    provider : 'Anthropic' | 'OpenAI'
    """
    if provider == "Anthropic":
        return AnthropicClient(api_key=api_key, model=model)
    elif provider == "OpenAI":
        return OpenAIClient(api_key=api_key, model=model)
    else:
        raise ValueError(f"Unknown provider: {provider!r}")
