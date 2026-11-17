"""Password hashing with PBKDF2-SHA256 (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PREFIX = "pbkdf2_sha256"
ITERATIONS = 600_000
# Stored hashes outside this range are refused: too few is too cheap to brute-force, too many
# would let a tampered row stall the server.
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 10_000_000


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


def _iterations(stored: str) -> int | None:
    try:
        return int(stored.split("$")[1])
    except (IndexError, ValueError):
        return None


def needs_rehash(stored: str) -> bool:
    """True when ``stored`` uses fewer iterations than new hashes get."""
    iterations = _iterations(stored) if is_hash(stored) else None
    return iterations is not None and iterations < ITERATIONS


def verify_password(password: str, stored: str) -> bool:
    """``stored`` must be a PBKDF2 hash made by :func:`hash_password`; anything else fails."""
    if not stored or not is_hash(stored):
        return False
    iterations = _iterations(stored)
    if iterations is None or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        return False
    try:
        _, _, salt_b64, digest_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
