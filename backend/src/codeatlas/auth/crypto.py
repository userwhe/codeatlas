"""Symmetric encryption for secrets stored at rest (GitHub user tokens, the OAuth state cookie)."""

from cryptography.fernet import Fernet, InvalidToken

from codeatlas.config import get_settings


class DecryptionError(Exception):
    """The ciphertext is invalid, tampered with, or expired."""


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())


def encrypt(text: str) -> bytes:
    return _fernet().encrypt(text.encode())


def decrypt(data: bytes, *, max_age_seconds: int | None = None) -> str:
    try:
        if max_age_seconds is None:
            return _fernet().decrypt(data).decode()
        return _fernet().decrypt(data, ttl=max_age_seconds).decode()
    except InvalidToken as exc:
        raise DecryptionError("invalid or expired ciphertext") from exc
