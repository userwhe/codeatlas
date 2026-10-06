"""The embedding boundary (research R9, R12, R13). Real and fake embedders follow this protocol.

Only documentation chunks and search queries are embedded. Texts and the API key are never logged.
"""

import hashlib
import logging
import math
import re
import time
from collections.abc import Callable, Sequence
from typing import Literal, Protocol

import voyageai.client
import voyageai.error

from codeatlas.config import Settings, get_settings
from codeatlas.providers.errors import ProviderUnavailable

logger = logging.getLogger(__name__)
# The Voyage SDK logs request bodies (the texts) at DEBUG level; keep them out of our logs.
logging.getLogger("voyage").setLevel(logging.INFO)

BATCH_SIZE = 128
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 4.0
# The SDK default is 600 seconds, longer than the question deadline.
REQUEST_TIMEOUT_SECONDS = 20.0

InputType = Literal["document", "query"]

# Transient failures worth another attempt. Any other error with a 429 or 5xx status also counts.
_RETRYABLE_ERRORS: tuple[type[voyageai.error.VoyageError], ...] = (
    voyageai.error.RateLimitError,
    voyageai.error.ServerError,
    voyageai.error.ServiceUnavailableError,
    voyageai.error.Timeout,
    voyageai.error.APIConnectionError,
    voyageai.error.TryAgain,
)

_WORD = re.compile(r"[^\W_]+")


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """One `embedding_dimensions`-float vector per text, in input order."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """The vector for a search query."""
        ...


class EmbeddingsResult(Protocol):
    @property
    def embeddings(self) -> Sequence[Sequence[float]]: ...


class VoyageClient(Protocol):
    """The part of `voyageai.Client` used here; tests pass a stub with the same method."""

    def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str,
        truncation: bool,
        output_dimension: int,
    ) -> EmbeddingsResult: ...


class VoyageEmbedder:
    """Voyage AI embeddings (research R12). Every failure surfaces as `ProviderUnavailable`."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: VoyageClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._sleep = sleep
        # The SDK's own retries stay off; `_embed_batch` retries with its own backoff.
        self._client: VoyageClient = (
            client
            if client is not None
            else voyageai.client.Client(
                api_key=settings.voyage_api_key, max_retries=0, timeout=REQUEST_TIMEOUT_SECONDS
            )
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._embed_batch(texts[start : start + BATCH_SIZE], "document"))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([text], "query")[0]

    def _embed_batch(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        attempt = 1
        while True:
            try:
                result = self._client.embed(
                    texts,
                    model=self._model,
                    input_type=input_type,
                    truncation=True,
                    output_dimension=self._dimensions,
                )
            except voyageai.error.VoyageError as exc:
                if not _retryable(exc):
                    raise ProviderUnavailable(_rejected_message(exc)) from exc
                if attempt == MAX_ATTEMPTS:
                    raise ProviderUnavailable(
                        f"Voyage embeddings failed after {MAX_ATTEMPTS} attempts: {_describe(exc)}"
                    ) from exc
                delay = min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
                logger.warning(
                    "Voyage embedding attempt %d of %d failed (%s); retrying in %.1f s",
                    attempt,
                    MAX_ATTEMPTS,
                    _describe(exc),
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue
            return self._vectors(result, len(texts))

    def _vectors(self, result: EmbeddingsResult, expected: int) -> list[list[float]]:
        vectors = [[float(value) for value in vector] for vector in result.embeddings]
        if len(vectors) != expected or any(len(v) != self._dimensions for v in vectors):
            raise ProviderUnavailable("Voyage returned an unexpected number or size of embeddings")
        return vectors


def _retryable(exc: voyageai.error.VoyageError) -> bool:
    if isinstance(exc, _RETRYABLE_ERRORS):
        return True
    status = exc.http_status
    return isinstance(status, int) and (status == 429 or status >= 500)


def _describe(exc: voyageai.error.VoyageError) -> str:
    status = exc.http_status
    return type(exc).__name__ + (f", HTTP {status}" if isinstance(status, int) else "")


def _rejected_message(exc: voyageai.error.VoyageError) -> str:
    message = f"Voyage rejected the embedding request ({_describe(exc)})"
    if isinstance(exc, voyageai.error.AuthenticationError):
        message += "; check VOYAGE_API_KEY"
    return message


class FakeEmbedder:
    """Deterministic bag-of-words hashing vectors: texts that share words have closer vectors.

    Set `fake_embedder_mode` to `unavailable` to make every call raise `ProviderUnavailable`.
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._check_available()
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self._check_available()
        return self._vector(text)

    def _check_available(self) -> None:
        # Read at call time, so tests can switch the mode on the live settings object.
        if self._settings.fake_embedder_mode == "unavailable":
            raise ProviderUnavailable("The fake embedder is configured as unavailable")

    def _vector(self, text: str) -> list[float]:
        dimensions = self._settings.embedding_dimensions
        vector = [0.0] * dimensions
        # A text without words still gets a unit vector, from its raw value.
        for word in _WORD.findall(text.lower()) or [text]:
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            vector[int.from_bytes(digest) % dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Return the fake embedder in fake mode, otherwise the Voyage embedder."""
    settings = settings or get_settings()
    if settings.fake_externals:
        return FakeEmbedder(settings)
    return VoyageEmbedder(settings)
