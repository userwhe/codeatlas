import pytest

from codeatlas.auth.crypto import DecryptionError, decrypt, encrypt


def test_round_trip() -> None:
    assert decrypt(encrypt("gho_example")) == "gho_example"


def test_tampered_ciphertext_is_rejected() -> None:
    data = bytearray(encrypt("secret"))
    data[-5] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt(bytes(data))


def test_different_ciphertexts_for_same_text() -> None:
    assert encrypt("same") != encrypt("same")
