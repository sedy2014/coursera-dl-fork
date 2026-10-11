"""Provider client tests using mocked SDKs without network requests."""

import inspect
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from slide_gen import llm_client
from slide_gen.llm_client import (
    ANTHROPIC_MODELS,
    MAX_INPUT_BYTES,
    OPENAI_MODELS,
    AnthropicClient,
    IncompleteGenerationError,
    OpenAIClient,
    check_request_budget,
    make_client,
)


@pytest.fixture(autouse=True)
def mock_sdks(monkeypatch):
    providers = {}
    for provider, module_name in (
        ("Anthropic", "anthropic"), ("OpenAI", "openai")
    ):
        stream = MagicMock()
        stream.text_stream = iter(())
        stream.get_final_message.return_value = SimpleNamespace(
            stop_reason="end_turn"
        )
        context = MagicMock()
        context.__enter__.return_value = stream
        request = Mock(return_value=context)
        if provider == "Anthropic":
            client = SimpleNamespace(messages=SimpleNamespace(stream=request))
        else:
            client = SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=request)
            ))
        constructor = Mock(return_value=client)
        monkeypatch.setitem(
            sys.modules, module_name,
            SimpleNamespace(**{provider: constructor}),
        )
        providers[provider] = SimpleNamespace(
            constructor=constructor, request=request,
            context=context, stream=stream,
        )
    return providers


def completion_chunk(content=None, finish_reason=None, refusal=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, refusal=refusal),
        finish_reason=finish_reason,
    )])


def test_model_identifiers_are_preserved():
    assert ANTHROPIC_MODELS == [
        "claude-sonnet-4-6", "claude-opus-5-5", "claude-opus-5", "claude-haiku-4-5",
    ]
    assert OPENAI_MODELS == ["gpt-4o", "gpt-oss-120b", "gpt-4o-mini", "o1", "o4-mini"]


def test_module_import_does_not_load_sdks(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setitem(sys.modules, "openai", None)
    namespace = runpy.run_path(llm_client.__file__)
    assert "AnthropicClient" in namespace
    assert "OpenAIClient" in namespace


@pytest.mark.parametrize("provider,client_type", [
    ("Anthropic", AnthropicClient), ("OpenAI", OpenAIClient),
])
def test_factory_configures_sdk(provider, client_type, mock_sdks):
    client = make_client(provider, "test-key", "test-model")
    assert isinstance(client, client_type)
    assert client._model == "test-model"
    mock_sdks[provider].constructor.assert_called_once_with(
        api_key="test-key", timeout=120.0, max_retries=2,
    )
    for other_provider, sdk in mock_sdks.items():
        if other_provider != provider:
            sdk.constructor.assert_not_called()


def test_factory_rejects_unknown_provider(mock_sdks):
    with pytest.raises(ValueError, match="Unknown provider"):
        make_client("unknown", "test-key", "test-model")
    for sdk in mock_sdks.values():
        sdk.constructor.assert_not_called()


def test_interfaces_have_no_effort_parameter():
    assert list(inspect.signature(make_client).parameters) == [
        "provider", "api_key", "model",
    ]
    for client_type in (AnthropicClient, OpenAIClient):
        assert list(inspect.signature(client_type).parameters) == [
            "api_key", "model",
        ]


@pytest.mark.parametrize("module_name,client_type", [
    ("anthropic", AnthropicClient), ("openai", OpenAIClient),
])
def test_missing_sdk_has_install_guidance(module_name, client_type, monkeypatch):
    monkeypatch.setitem(sys.modules, module_name, None)
    with pytest.raises(ImportError, match=f"pip install {module_name}"):
        client_type("test-key")


@pytest.mark.parametrize("system,user", [
    ("", ""),
    ("s" * 24000, "u" * 24000),
    ("", "a" * 48000),
    ("a" * 48000, ""),
    ("é" * 12000, "界" * 8000),
    ("", "😀" * 12000),
], ids=["empty", "combined", "user", "system", "multibyte", "emoji"])
def test_request_budget_accepts_boundary(system, user):
    assert MAX_INPUT_BYTES == 48000
    assert check_request_budget(system, user) is None


@pytest.mark.parametrize("system,user", [
    ("s", "u" * 48000),
    ("s" * 48001, ""),
    ("", "é" * 24001),
    ("", "😀" * 12001),
], ids=["combined", "system", "multibyte", "emoji"])
def test_request_budget_rejects_overflow(system, user):
    with pytest.raises(ValueError, match="UTF-8 bytes.*48000"):
        check_request_budget(system, user)


@pytest.mark.parametrize("provider", ["Anthropic", "OpenAI"])
@pytest.mark.parametrize("method", ["stream", "complete"])
def test_budget_checked_before_request(provider, method, mock_sdks):
    client = make_client(provider, "test-key", "test-model")
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        result = getattr(client, method)("s", "u" * MAX_INPUT_BYTES)
        if method == "stream":
            list(result)
    mock_sdks[provider].request.assert_not_called()


def test_anthropic_stream_request_and_final_message(mock_sdks):
    sdk = mock_sdks["Anthropic"]
    sdk.stream.text_stream = iter(["", "first", " second"])
    chunks = AnthropicClient("test-key", "test-model").stream("system", "user")
    assert next(chunks) == "first"
    sdk.stream.get_final_message.assert_not_called()
    assert list(chunks) == [" second"]
    sdk.stream.get_final_message.assert_called_once_with()
    sdk.request.assert_called_once_with(
        model="test-model", max_tokens=16000, system="system",
        messages=[{"role": "user", "content": "user"}],
    )
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("reason", [
    "max_tokens", "refusal", "stop_sequence", "tool_use", "pause_turn",
    "model_context_window_exceeded", "unknown", None,
])
def test_anthropic_rejects_abnormal_stop_after_text(reason, mock_sdks):
    sdk = mock_sdks["Anthropic"]
    sdk.stream.text_stream = iter(["partial"])
    sdk.stream.get_final_message.return_value = SimpleNamespace(stop_reason=reason)
    chunks = AnthropicClient("test-key").stream("system", "user")
    assert next(chunks) == "partial"
    with pytest.raises(IncompleteGenerationError, match="did not complete"):
        list(chunks)
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("message", [None, SimpleNamespace()])
def test_anthropic_rejects_missing_final_signal(message, mock_sdks):
    sdk = mock_sdks["Anthropic"]
    sdk.stream.text_stream = iter(["partial"])
    sdk.stream.get_final_message.return_value = message
    with pytest.raises(IncompleteGenerationError):
        AnthropicClient("test-key").complete("system", "user")


def test_anthropic_empty_stream_final_message_error(mock_sdks):
    sdk = mock_sdks["Anthropic"]
    error = RuntimeError("No message was received")
    sdk.stream.get_final_message.side_effect = error
    with pytest.raises(IncompleteGenerationError) as caught:
        AnthropicClient("test-key").complete("system", "user")
    assert caught.value.__cause__ is error
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("model", OPENAI_MODELS)
def test_openai_stream_request_and_empty_choices(model, mock_sdks):
    sdk = mock_sdks["OpenAI"]
    sdk.stream.__iter__.return_value = iter([
        SimpleNamespace(choices=[]),
        completion_chunk(),
        completion_chunk("first"),
        completion_chunk(" second", finish_reason="stop"),
        SimpleNamespace(choices=[]),
    ])
    assert list(OpenAIClient("test-key", model).stream("system", "user")) == [
        "first", " second",
    ]
    sdk.request.assert_called_once_with(
        model=model,
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        max_completion_tokens=16000, stream=True,
    )
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("reason", [
    "length", "content_filter", "tool_calls", "function_call", "unknown", None,
])
def test_openai_rejects_abnormal_stop_after_text(reason, mock_sdks):
    sdk = mock_sdks["OpenAI"]
    sdk.stream.__iter__.return_value = iter([
        completion_chunk("partial", finish_reason=reason),
        completion_chunk(finish_reason="stop"),
    ] if reason is not None else [completion_chunk("partial")])
    chunks = OpenAIClient("test-key").stream("system", "user")
    assert next(chunks) == "partial"
    with pytest.raises(IncompleteGenerationError):
        list(chunks)
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("refusal", ["Request refused", ""])
def test_openai_refusal_cannot_be_successful(mock_sdks, refusal):
    sdk = mock_sdks["OpenAI"]
    sdk.stream.__iter__.return_value = iter([
        completion_chunk("partial"),
        completion_chunk(refusal=refusal),
        completion_chunk(finish_reason="stop"),
    ])
    chunks = OpenAIClient("test-key").stream("system", "user")
    assert next(chunks) == "partial"
    with pytest.raises(IncompleteGenerationError, match="refused"):
        list(chunks)
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("chunks", [
    [],
    [SimpleNamespace(choices=[])],
    [completion_chunk("partial"), SimpleNamespace(choices=[])],
    [SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content="partial"),
    )])],
])
def test_openai_requires_explicit_finish_reason(chunks, mock_sdks):
    mock_sdks["OpenAI"].stream.__iter__.return_value = iter(chunks)
    with pytest.raises(IncompleteGenerationError, match="finish reason"):
        OpenAIClient("test-key").complete("system", "user")


def test_openai_accepts_terminal_choice_without_delta(mock_sdks):
    mock_sdks["OpenAI"].stream.__iter__.return_value = iter([
        completion_chunk("text"),
        SimpleNamespace(choices=[SimpleNamespace(
            delta=None, finish_reason="stop",
        )]),
    ])
    assert OpenAIClient("test-key").complete("system", "user") == "text"


@pytest.mark.parametrize("provider", ["Anthropic", "OpenAI"])
@pytest.mark.parametrize("texts", [[], [""], [" ", "\n\t"]])
def test_successful_stop_requires_nonempty_text(provider, texts, mock_sdks):
    sdk = mock_sdks[provider]
    sdk.stream.text_stream = iter(texts)
    sdk.stream.__iter__.return_value = iter(
        [completion_chunk(text) for text in texts]
        + [completion_chunk(finish_reason="stop")]
    )
    with pytest.raises(IncompleteGenerationError, match="empty text"):
        make_client(provider, "test-key", "test-model").complete("s", "u")
    sdk.context.__exit__.assert_called_once()


@pytest.mark.parametrize("provider", ["Anthropic", "OpenAI"])
def test_complete_joins_validated_text(provider, mock_sdks):
    sdk = mock_sdks[provider]
    sdk.stream.text_stream = iter(["first", " second"])
    sdk.stream.__iter__.return_value = iter([
        completion_chunk("first"), completion_chunk(" second"),
        completion_chunk(finish_reason="stop"),
    ])
    client = make_client(provider, "test-key", "test-model")
    assert client.complete("system", "user") == "first second"


@pytest.mark.parametrize("provider", ["Anthropic", "OpenAI"])
def test_stream_failure_propagates_and_closes_context(provider, mock_sdks):
    error = ConnectionError("Stream interrupted")

    def interrupted_stream():
        yield "partial" if provider == "Anthropic" else completion_chunk("partial")
        raise error

    sdk = mock_sdks[provider]
    sdk.stream.text_stream = interrupted_stream()
    sdk.stream.__iter__.return_value = interrupted_stream()
    client = make_client(provider, "test-key", "test-model")
    chunks = client.stream("system", "user")
    assert next(chunks) == "partial"
    with pytest.raises(ConnectionError) as caught:
        list(chunks)
    assert caught.value is error
    sdk.context.__exit__.assert_called_once()
    sdk.request.assert_called_once()
