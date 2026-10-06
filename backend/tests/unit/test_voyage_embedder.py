"""The Voyage embedder against a stubbed client (no network), the fake embedder, and the factory."""

import logging
import math
from types import SimpleNamespace
from typing import Any

import pytest
import voyageai.error as voyage_errors

from codeatlas.config import Settings
from codeatlas.providers.embeddings import FakeEmbedder, VoyageEmbedder, get_embedder
from codeatlas.providers.errors import ProviderUnavailable

API_KEY = "voyage-test-key"
DIMENSIONS = 1024


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, voyage_api_key=API_KEY, **values)  # type: ignore[call-arg]


class StubClient:
    """Records `embed` calls, raises the queued errors first, then returns one vector per text.

    The first component of each vector is the text's position across all calls, so tests can
    check that order survives batching.
    """

    def __init__(self, *errors: Exception, dimensions: int | None = None) -> None:
        self.errors = list(errors)
        self.dimensions = dimensions
        self.calls: list[dict[str, Any]] = []
        self.seen = 0

    def embed(self, texts: list[str], **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"texts": list(texts), **kwargs})
        if self.errors:
            raise self.errors.pop(0)
        size = self.dimensions or kwargs["output_dimension"]
        vectors = []
        for _ in texts:
            vectors.append([float(self.seen)] + [0.0] * (size - 1))
            self.seen += 1
        return SimpleNamespace(embeddings=vectors)


@pytest.fixture
def sleeps() -> list[float]:
    return []


def voyage(client: StubClient, sleeps: list[float]) -> VoyageEmbedder:
    return VoyageEmbedder(make_settings(), client=client, sleep=sleeps.append)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


# Voyage embedder


def test_documents_are_sent_in_ordered_batches_of_at_most_128(sleeps: list[float]) -> None:
    client = StubClient()
    texts = [f"chunk {i}" for i in range(300)]

    vectors = voyage(client, sleeps).embed_documents(texts)

    assert [len(call["texts"]) for call in client.calls] == [128, 128, 44]
    assert [text for call in client.calls for text in call["texts"]] == texts
    assert [vector[0] for vector in vectors] == [float(i) for i in range(300)]
    assert all(len(vector) == DIMENSIONS for vector in vectors)


def test_documents_use_document_input_type_model_and_dimensions(sleeps: list[float]) -> None:
    client = StubClient()

    voyage(client, sleeps).embed_documents(["## Setup", "## Usage"])

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["input_type"] == "document"
    assert call["model"] == "voyage-4"
    assert call["output_dimension"] == DIMENSIONS
    assert call["truncation"] is True


def test_query_uses_query_input_type(sleeps: list[float]) -> None:
    client = StubClient()

    vector = voyage(client, sleeps).embed_query("how do I run the setup?")

    assert len(vector) == DIMENSIONS
    assert client.calls == [
        {
            "texts": ["how do I run the setup?"],
            "model": "voyage-4",
            "input_type": "query",
            "truncation": True,
            "output_dimension": DIMENSIONS,
        }
    ]


def test_empty_document_list_makes_no_call(sleeps: list[float]) -> None:
    client = StubClient()

    assert voyage(client, sleeps).embed_documents([]) == []
    assert client.calls == []


RETRYABLE_ERRORS = [
    voyage_errors.RateLimitError("rate limited", http_status=429),
    voyage_errors.ServerError("server error", http_status=500),
    voyage_errors.ServiceUnavailableError("overloaded", http_status=503),
    voyage_errors.Timeout("Request timed out"),
    voyage_errors.APIConnectionError("Error communicating with VoyageAI"),
    voyage_errors.APIError("bad gateway", http_status=502),
]


@pytest.mark.parametrize("error", RETRYABLE_ERRORS, ids=lambda e: type(e).__name__)
def test_retryable_errors_are_retried_then_raise_provider_unavailable(
    error: Exception, sleeps: list[float]
) -> None:
    client = StubClient(error, error, error)

    with pytest.raises(ProviderUnavailable, match="after 3 attempts"):
        voyage(client, sleeps).embed_documents(["## Setup"])

    assert len(client.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_success_after_one_transient_failure(sleeps: list[float]) -> None:
    client = StubClient(voyage_errors.RateLimitError("rate limited", http_status=429))

    vector = voyage(client, sleeps).embed_query("setup")

    assert len(vector) == DIMENSIONS
    assert len(client.calls) == 2
    assert sleeps == [0.5]


NON_RETRYABLE_ERRORS = [
    voyage_errors.InvalidRequestError("bad request", http_status=400),
    voyage_errors.AuthenticationError("invalid key", http_status=401),
    voyage_errors.MalformedRequestError("unprocessable", http_status=422),
    voyage_errors.APIError("forbidden", http_status=403),
]


@pytest.mark.parametrize("error", NON_RETRYABLE_ERRORS, ids=lambda e: type(e).__name__)
def test_non_retryable_errors_raise_provider_unavailable_at_once(
    error: Exception, sleeps: list[float]
) -> None:
    client = StubClient(error)

    with pytest.raises(ProviderUnavailable, match="rejected"):
        voyage(client, sleeps).embed_documents(["## Setup"])

    assert len(client.calls) == 1
    assert sleeps == []


def test_authentication_error_names_the_key_setting(sleeps: list[float]) -> None:
    client = StubClient(voyage_errors.AuthenticationError("invalid key", http_status=401))

    with pytest.raises(ProviderUnavailable, match="VOYAGE_API_KEY"):
        voyage(client, sleeps).embed_query("setup")


def test_wrong_vector_size_raises_provider_unavailable(sleeps: list[float]) -> None:
    client = StubClient(dimensions=512)

    with pytest.raises(ProviderUnavailable, match="unexpected"):
        voyage(client, sleeps).embed_documents(["## Setup"])


def test_texts_and_key_are_not_logged(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    error = voyage_errors.ServiceUnavailableError("overloaded", http_status=503)
    client = StubClient(error, error, error)

    with pytest.raises(ProviderUnavailable) as raised:
        voyage(client, sleeps).embed_documents(["private design notes"])

    assert caplog.records, "retries should be logged"
    for text in (caplog.text, str(raised.value)):
        assert "private design notes" not in text
        assert API_KEY not in text


def test_real_client_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    def fake_client(**kwargs: Any) -> StubClient:
        created.append(kwargs)
        return StubClient()

    monkeypatch.setattr("voyageai.client.Client", fake_client)

    VoyageEmbedder(make_settings())

    assert len(created) == 1
    assert created[0]["api_key"] == API_KEY
    assert created[0]["max_retries"] == 0


# Fake embedder


def test_fake_vectors_are_deterministic_unit_vectors() -> None:
    first = FakeEmbedder(make_settings()).embed_query("Install the dependencies")
    second = FakeEmbedder(make_settings()).embed_documents(["install the DEPENDENCIES"])[0]

    assert first == second
    assert len(first) == DIMENSIONS
    assert math.isclose(math.sqrt(cosine(first, first)), 1.0)


def test_fake_vector_for_text_without_words_is_a_unit_vector() -> None:
    for text in ("", "  ", "---"):
        vector = FakeEmbedder(make_settings()).embed_query(text)
        assert math.isclose(math.sqrt(cosine(vector, vector)), 1.0)


def test_fake_vectors_rank_texts_sharing_words_closer() -> None:
    embedder = FakeEmbedder(make_settings())
    setup, license_, usage = embedder.embed_documents(
        [
            "## Setup\n\nRun the setup script to install the dependencies.",
            "## License\n\nReleased under the MIT license.",
            "## Usage\n\nCall the client from your application code.",
        ]
    )
    query = embedder.embed_query("setup")

    assert cosine(query, setup) > cosine(query, license_)
    assert cosine(query, setup) > cosine(query, usage)


def test_fake_unavailable_mode_is_read_at_call_time(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = get_embedder()
    embedder.embed_query("setup")

    monkeypatch.setattr(settings, "fake_embedder_mode", "unavailable")

    with pytest.raises(ProviderUnavailable):
        embedder.embed_query("setup")
    with pytest.raises(ProviderUnavailable):
        embedder.embed_documents([])


# Factory


def test_get_embedder_returns_the_fake_in_fake_mode(settings: Settings) -> None:
    assert settings.fake_externals is True
    assert isinstance(get_embedder(), FakeEmbedder)
    assert isinstance(get_embedder(settings), FakeEmbedder)


def test_get_embedder_returns_voyage_outside_fake_mode() -> None:
    settings = make_settings(CODEATLAS_FAKE_EXTERNALS=False)

    assert isinstance(get_embedder(settings), VoyageEmbedder)
