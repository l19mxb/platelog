"""Salted password verifiers for Admin and camera credentials."""

import hashlib
import hmac
import secrets
from threading import Lock


_password_lock = Lock()


def password_key(password: str, salt: bytes) -> bytes:
    with _password_lock:
        return hashlib.scrypt(password.encode(), salt=salt, n=2**16, r=8, p=2, maxmem=96 * 1024**2)


def password_verifier(password: str) -> str:
    if not 14 <= len(password) or len(password.encode()) > 1024:
        raise ValueError("Use at least 14 characters and no more than 1024 bytes.")
    salt = secrets.token_bytes(16)
    key = password_key(password, salt)
    return f"{salt.hex()}:{key.hex()}"


def password_matches(password: str, verifier: str) -> bool:
    if not password or len(password.encode()) > 1024:
        return False
    salt, expected = verifier.split(":", 1)
    return hmac.compare_digest(password_key(password, bytes.fromhex(salt)), bytes.fromhex(expected))
