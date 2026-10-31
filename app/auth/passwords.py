"""Password hashing with PBKDF2-SHA256 (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PREFIX = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        [
            PREFIX,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def is_hash(value: str) -> bool:
    return value.startswith(PREFIX + "$") and value.count("$") == 3


def verify_password(password: str, stored: str) -> bool:
    """``stored`` is either a PBKDF2 hash or (for convenience) the plain password."""
    if not stored:
        return False
    if not is_hash(stored):
        return hmac.compare_digest(password.encode(), stored.encode())
    try:
        _, iterations, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
