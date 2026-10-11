"""
slide_gen/llm_client.py
-----------------------
Thin wrappers around the Anthropic and OpenAI SDKs.
Both clients expose the same interface so slide_tab.py doesn't need
to branch on provider.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class LLMClient:
    """Interface all provider clients must implement."""

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
            stop_reason = getattr(message, "stop_reason", None)
            if stop_reason != "end_turn":
                raise IncompleteGenerationError(
                    f"Anthropic generation did not complete: {stop_reason!r}."
                )
            if not has_text:
                raise IncompleteGenerationError(
                    "Anthropic generation returned empty text."
                )


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
        with self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_completion_tokens=16000,
            stream=True,
        ) as stream:
            for chunk in stream:
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
