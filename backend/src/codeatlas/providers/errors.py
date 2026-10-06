"""Errors raised by external model-provider adapters (embeddings and answer generation)."""


class ProviderUnavailable(Exception):
    """The provider failed transiently (429, 5xx, timeout) after the adapter's own retries."""


class ProviderRefused(Exception):
    """The provider declined the request, for example because of a safety block."""
