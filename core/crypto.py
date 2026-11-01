"""Encryption for connection credentials; there is no plaintext fallback."""

from cryptography.fernet import Fernet, InvalidToken

from core.config import get_settings


class CryptoError(ValueError):
    pass


def cipher() -> Fernet:
    key = get_settings().fernet_key.get_secret_value()
    if not key:
        raise CryptoError("FERNET_KEY is missing; configure it before connecting an institution")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise CryptoError("FERNET_KEY is invalid; configure a valid Fernet key") from None


def encrypt(text: str) -> str:
    return cipher().encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    encryption = cipher()
    try:
        return encryption.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        raise CryptoError("Stored credentials cannot be decrypted with FERNET_KEY") from None


def token_unreadable(token: str) -> bool:
    """Only a valid configured key can establish that a saved token is unreadable."""
    try:
        cipher()
    except CryptoError:
        return False
    try:
        decrypt(token)
    except CryptoError:
        return True
    return False
