"""The Gemini answer model against a stubbed client (no network), the fake, and the factory."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from google import genai
from google.genai._gaos.lib.compat_errors import APIConnectionError, APIError, APITimeoutError
from google.genai.interactions import Interaction

from codeatlas.config import Settings
from codeatlas.providers.answer_model import (
    FAKE_MODEL,
    RESPONSE_SCHEMA,
    AnswerUsage,
    FakeAnswerModel,
    GeminiAnswerModel,
    get_answer_model,
    reset_fake_answer_model,
)
from codeatlas.providers.errors import ProviderRefused, ProviderUnavailable
from codeatlas.qa.schema import AnswerOutput

API_KEY = "gemini-test-key"
SYSTEM = "Answer only from the evidence."
SECRET = "private source excerpt"
USER_CONTENT = (
    f'<evidence id="E1" type="code" path="app/auth.py" lines="1-9">\n{SECRET}\n</evidence>\n\n'
    "<question>\nHow are requests authorized?\n</question>"
)
VALID_OUTPUT: dict[str, Any] = {
    "status": "answered",
    "summary": "Requests are checked by an access function.",
    "claims": [
        {"text": "Access is checked per repository.", "kind": "fact", "evidence_ids": ["E1"]}
    ],
    "gaps": [],
}
USAGE = {
    "total_input_tokens": 1200,
    "total_output_tokens": 300,
    "total_thought_tokens": 450,
    "total_cached_tokens": 64,
    "total_tokens": 1950,
}
REQUEST = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/interactions")


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **{"gemini_api_key": API_KEY, **values})  # type: ignore[arg-type]


def interaction_body(
    text: str | None = json.dumps(VALID_OUTPUT),
    *,
    status: str = "completed",
    errors: list[dict[str, str]] | None = None,
    usage: dict[str, int] | None = USAGE,
) -> dict[str, Any]:
    """An Interactions API response body; `text` is the model's output, or None for no output."""
    steps: list[dict[str, Any]] = [{"type": "thought", "signature": "opaque"}]
    if text is not None:
        steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    return {
        "id": "interaction-1",
        "status": status,
        "steps": steps,
        "errors": errors,
        "usage": usage,
    }


def interaction(text: str | None = json.dumps(VALID_OUTPUT), **fields: Any) -> Interaction:
    # The SDK derives `output_text` from the trailing model-output steps while validating.
    return Interaction.model_validate(interaction_body(text, **fields))


def http_error(status: int, code: str = "api_error") -> APIError:
    """The SDK's own error for an HTTP status, as `client.interactions.create` raises it."""
    body = {"error": {"code": code, "message": "details from the API"}}
    return APIError.generate(status, body, None, httpx.Response(status, request=REQUEST))


class StubClient:
    """Records `interactions.create` calls, raises the queued errors first, then responds."""

    def __init__(self, *errors: Exception, response: Interaction | None = None) -> None:
        self.errors = list(errors)
        self.response = response or interaction()
        self.calls: list[dict[str, Any]] = []

    @property
    def interactions(self) -> "StubClient":
        return self

    def create(self, **kwargs: Any) -> Interaction:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.response


@pytest.fixture
def sleeps() -> list[float]:
    return []


def gemini(client: StubClient, sleeps: list[float], **settings: object) -> GeminiAnswerModel:
    return GeminiAnswerModel(make_settings(**settings), client=client, sleep=sleeps.append)


def ask(model: GeminiAnswerModel) -> Any:
    return model.answer(system=SYSTEM, user_content=USER_CONTENT)


# Request shape


def test_request_is_stateless_with_json_schema_and_thinking_level_and_no_tools(
    sleeps: list[float],
) -> None:
    client = StubClient()

    ask(gemini(client, sleeps))

    assert client.calls == [
        {
            "model": "gemini-3.8-flash",
            "input": USER_CONTENT,
            "system_instruction": SYSTEM,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": RESPONSE_SCHEMA,
            },
            "generation_config": {"thinking_level": "medium", "max_output_tokens": 16000},
            "store": False,
        }
    ]
    assert "tools" not in client.calls[0]


def test_request_follows_the_configured_model_and_thinking_level(sleeps: list[float]) -> None:
    client = StubClient()

    ask(gemini(client, sleeps, answer_model="gemini-3.7-flash", answer_thinking_level="high"))

    assert client.calls[0]["model"] == "gemini-3.7-flash"
    assert client.calls[0]["generation_config"]["thinking_level"] == "high"


def test_response_schema_is_answer_output_without_unsupported_keywords() -> None:
    claim = RESPONSE_SCHEMA["properties"]["claims"]

    assert set(RESPONSE_SCHEMA["properties"]) == {"status", "summary", "claims", "gaps"}
    assert RESPONSE_SCHEMA["required"] == ["status", "summary"]
    assert RESPONSE_SCHEMA["properties"]["status"]["enum"] == ["answered", "insufficient_evidence"]
    assert claim["maxItems"] == 10
    assert claim["items"]["properties"]["kind"]["enum"] == ["fact", "inference"]
    assert claim["items"]["required"] == ["text", "kind"]
    for keyword in ("$ref", "$defs", "title", "maxLength", "minLength"):
        assert keyword not in json.dumps(RESPONSE_SCHEMA)


# Parsing


def test_valid_json_parses_into_answer_output(sleeps: list[float]) -> None:
    result = ask(gemini(StubClient(), sleeps))

    assert result.output == AnswerOutput.model_validate(VALID_OUTPUT)
    assert result.parse_error is None
    assert result.model == "gemini-3.8-flash"


TOO_MANY_CLAIMS = {**VALID_OUTPUT, "claims": VALID_OUTPUT["claims"] * 11}
CLAIM_TOO_LONG = {**VALID_OUTPUT, "claims": [{"text": SECRET * 40, "kind": "fact"}]}
UNPARSABLE = {
    "not json": (f"Sure! {SECRET}", "Invalid JSON"),
    "missing summary": (json.dumps({"status": "answered"}), "summary"),
    "unknown status": (json.dumps({**VALID_OUTPUT, "status": SECRET}), "status"),
    "too many claims": (json.dumps(TOO_MANY_CLAIMS), "claims"),
    "claim too long": (json.dumps(CLAIM_TOO_LONG), "claims.0.text"),
    "empty": ("", "empty"),
    "missing": (None, "empty"),
}


@pytest.mark.parametrize(("text", "reason"), UNPARSABLE.values(), ids=UNPARSABLE.keys())
def test_unparsable_output_is_reported_as_a_validation_error(
    text: str | None, reason: str, sleeps: list[float]
) -> None:
    result = ask(gemini(StubClient(response=interaction(text)), sleeps))

    assert result.output is None
    assert result.parse_error is not None and reason in result.parse_error
    assert SECRET not in result.parse_error
    assert result.usage.input_tokens == 1200


def test_output_cut_off_at_the_token_limit_is_reported(sleeps: list[float]) -> None:
    truncated = json.dumps(VALID_OUTPUT)[:40]

    result = ask(gemini(StubClient(response=interaction(truncated, status="incomplete")), sleeps))

    assert result.output is None
    assert result.parse_error is not None and "token limit" in result.parse_error


# Blocks and failures


@pytest.mark.parametrize("code", ["safety", "prohibited_content", "spii", "recitation"])
def test_blocked_interaction_raises_provider_refused(code: str, sleeps: list[float]) -> None:
    response = interaction("", status="failed", errors=[{"code": code, "message": "blocked"}])
    client = StubClient(response=response)

    with pytest.raises(ProviderRefused, match=code):
        ask(gemini(client, sleeps))

    assert len(client.calls) == 1


def test_blocked_request_error_raises_provider_refused(sleeps: list[float]) -> None:
    client = StubClient(http_error(400, "safety"))

    with pytest.raises(ProviderRefused, match="blocked"):
        ask(gemini(client, sleeps))

    assert len(client.calls) == 1
    assert sleeps == []


def test_failed_interaction_without_a_block_raises_provider_unavailable(
    sleeps: list[float],
) -> None:
    response = interaction(None, status="failed", errors=[{"code": "api_error"}])

    with pytest.raises(ProviderUnavailable, match="status failed"):
        ask(gemini(StubClient(response=response), sleeps))


RETRYABLE_ERRORS: dict[str, Callable[[], Exception]] = {
    "429": lambda: http_error(429, "rate_limit_exceeded"),
    "500": lambda: http_error(500),
    "503": lambda: http_error(503, "service_unavailable"),
    "504": lambda: http_error(504, "deadline_exceeded"),
    "timeout": lambda: APITimeoutError(request=REQUEST),
    "connection": lambda: APIConnectionError(request=REQUEST),
}


@pytest.mark.parametrize("make_error", RETRYABLE_ERRORS.values(), ids=RETRYABLE_ERRORS.keys())
def test_transient_errors_are_retried_with_capped_backoff_then_raise_provider_unavailable(
    make_error: Callable[[], Exception], sleeps: list[float]
) -> None:
    client = StubClient(make_error(), make_error(), make_error())

    with pytest.raises(ProviderUnavailable, match="after 3 attempts"):
        ask(gemini(client, sleeps))

    assert len(client.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_success_after_one_transient_failure(sleeps: list[float]) -> None:
    client = StubClient(http_error(429, "rate_limit_exceeded"))

    result = ask(gemini(client, sleeps))

    assert result.output == AnswerOutput.model_validate(VALID_OUTPUT)
    assert len(client.calls) == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_raise_provider_unavailable_at_once(status: int, sleeps: list[float]) -> None:
    client = StubClient(http_error(status, "invalid_request"))

    with pytest.raises(ProviderUnavailable, match=f"rejected .*HTTP {status}"):
        ask(gemini(client, sleeps))

    assert len(client.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_errors_name_the_key_setting(status: int, sleeps: list[float]) -> None:
    client = StubClient(http_error(status, "authentication"))

    with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
        ask(gemini(client, sleeps))


def test_missing_key_raises_provider_unavailable_without_a_request() -> None:
    model = GeminiAnswerModel(make_settings(gemini_api_key=""))

    with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
        ask(model)


# Usage


def test_token_counts_are_recorded_in_usage(sleeps: list[float]) -> None:
    result = ask(gemini(StubClient(), sleeps))

    assert result.usage == AnswerUsage(
        input_tokens=1200, output_tokens=300, thinking_tokens=450, cached_tokens=64
    )


def test_missing_token_counts_are_zero(sleeps: list[float]) -> None:
    response = interaction(usage={"total_input_tokens": 900})

    result = ask(gemini(StubClient(response=response), sleeps))

    assert result.usage == AnswerUsage(
        input_tokens=900, output_tokens=0, thinking_tokens=0, cached_tokens=0
    )
    result = ask(gemini(StubClient(response=interaction(usage=None)), sleeps))
    assert result.usage == AnswerUsage(0, 0, 0, 0)


def test_prompt_output_and_key_stay_out_of_logs_and_errors(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    error = http_error(503, "service_unavailable")

    with pytest.raises(ProviderUnavailable) as raised:
        ask(gemini(StubClient(error, error, error), sleeps))
    result = ask(gemini(StubClient(response=interaction(f"not json {SECRET}")), sleeps))

    assert caplog.records, "retries should be logged"
    for text in (caplog.text, str(raised.value), str(result.parse_error)):
        assert SECRET not in text
        assert SYSTEM not in text
        assert API_KEY not in text


# The real SDK client, through an in-memory transport (no network)


def use_mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    build = genai.Client

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def client_with_mock_transport(**kwargs: Any) -> genai.Client:
        transport = httpx.Client(transport=httpx.MockTransport(record))
        options = kwargs["http_options"].model_copy(update={"httpx_client": transport})
        return build(**{**kwargs, "http_options": options})

    monkeypatch.setattr(genai, "Client", client_with_mock_transport)
    return requests


def test_real_client_sends_the_verified_request_shape(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    body = interaction_body()
    requests = use_mock_transport(monkeypatch, lambda _: httpx.Response(200, json=body))

    result = GeminiAnswerModel(make_settings(), sleep=sleeps.append).answer(
        system=SYSTEM, user_content=USER_CONTENT
    )

    assert len(requests) == 1
    assert requests[0].url.path == "/v1beta/interactions"
    assert requests[0].headers["x-goog-api-key"] == API_KEY
    assert json.loads(requests[0].content) == {
        "model": "gemini-3.8-flash",
        "input": USER_CONTENT,
        "system_instruction": SYSTEM,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": RESPONSE_SCHEMA,
        },
        "generation_config": {"thinking_level": "medium", "max_output_tokens": 16000},
        "store": False,
    }
    assert result.output == AnswerOutput.model_validate(VALID_OUTPUT)
    assert result.usage == AnswerUsage(1200, 300, 450, 64)


def test_real_client_retries_only_in_the_adapter(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    error = {"error": {"code": "service_unavailable", "message": "overloaded"}}
    requests = use_mock_transport(monkeypatch, lambda _: httpx.Response(503, json=error))

    with pytest.raises(ProviderUnavailable, match="InternalServerError, HTTP 503"):
        GeminiAnswerModel(make_settings(), sleep=sleeps.append).answer(
            system=SYSTEM, user_content=USER_CONTENT
        )

    assert len(requests) == 3
    assert sleeps == [1.0, 2.0]


# Fake answer model


def fake_answer(settings: Settings, content: str = USER_CONTENT) -> Any:
    return get_answer_model().answer(system=SYSTEM, user_content=content)


TWO_ITEMS = (
    USER_CONTENT + '\n\n<evidence id="E2" type="doc" path="README.md" lines="1-5">\nx\n</evidence>'
)


def test_fake_ok_cites_the_first_items(settings: Settings) -> None:
    result = fake_answer(settings, TWO_ITEMS)

    assert result.output.status == "answered"
    assert [(c.kind, c.evidence_ids) for c in result.output.claims] == [
        ("fact", ["E1"]),
        ("inference", ["E2"]),
    ]
    assert result.parse_error is None
    assert result.model == FAKE_MODEL
    assert result.usage.input_tokens > 0 and result.usage.output_tokens > 0


def test_fake_ok_with_one_item_gives_one_fact(settings: Settings) -> None:
    result = fake_answer(settings)

    assert [(c.kind, c.evidence_ids) for c in result.output.claims] == [("fact", ["E1"])]


def test_fake_ok_ignores_labels_inside_excerpts(settings: Settings) -> None:
    content = '<evidence id="E1" path="a.html" lines="1-1">\n<div id="E7"></div>\n</evidence>'

    result = fake_answer(settings, content)

    assert [c.evidence_ids for c in result.output.claims] == [["E1"]]


def test_fake_ok_without_evidence_is_insufficient(settings: Settings) -> None:
    result = fake_answer(settings, "<question>\nanything\n</question>")

    assert result.output.status == "insufficient_evidence"
    assert result.output.claims == []
    assert len(result.output.gaps) == 1


def test_fake_insufficient(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fake_answer_model_mode", "insufficient")

    result = fake_answer(settings)

    assert result.output.status == "insufficient_evidence"
    assert result.output.claims == []
    assert len(result.output.gaps) == 1


def test_fake_unavailable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fake_answer_model_mode", "unavailable")

    with pytest.raises(ProviderUnavailable):
        fake_answer(settings)


def test_fake_refusal(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fake_answer_model_mode", "refusal")

    with pytest.raises(ProviderRefused):
        fake_answer(settings)


def test_fake_invalid_citations_on_every_call(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "fake_answer_model_mode", "invalid_citations")

    for _ in range(2):
        output = fake_answer(settings).output
        assert output.status == "answered"
        assert [(c.kind, c.evidence_ids) for c in output.claims] == [("fact", ["E99"])]


def test_fake_counts_calls_including_failures(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_answer(settings)
    monkeypatch.setattr(settings, "fake_answer_model_mode", "unavailable")
    with pytest.raises(ProviderUnavailable):
        fake_answer(settings)

    model = get_answer_model()
    assert isinstance(model, FakeAnswerModel)
    assert model.calls == 2


# Factory


def test_get_answer_model_returns_one_shared_fake_in_fake_mode(settings: Settings) -> None:
    assert settings.fake_externals is True
    model = get_answer_model()

    assert isinstance(model, FakeAnswerModel)
    assert get_answer_model(settings) is model


def test_reset_starts_a_new_fake_at_zero_calls(settings: Settings) -> None:
    fake_answer(settings)

    reset_fake_answer_model()

    model = get_answer_model()
    assert isinstance(model, FakeAnswerModel)
    assert model.calls == 0


def test_get_answer_model_returns_gemini_outside_fake_mode() -> None:
    settings = make_settings(CODEATLAS_FAKE_EXTERNALS=False)

    assert isinstance(get_answer_model(settings), GeminiAnswerModel)
