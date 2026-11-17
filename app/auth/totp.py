"""Time-based one-time passwords (RFC 6238) without third-party dependencies."""

from __future__ import annotations

import base64
import hmac
import re
import secrets
import struct
import time
from hashlib import sha1
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
_CODE_RE = re.compile(rf"[0-9]{{{DIGITS}}}")  # ASCII digits only, not other Unicode digits
SECRET_RE = re.compile(r"[A-Z2-7]{32}")  # what new_secret() makes


def new_secret() -> str:
    """A fresh 160-bit secret in base32, the format authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _decode(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    cleaned += "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned, casefold=True)


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_decode(secret), struct.pack(">Q", counter), sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_counter(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def verify(
    secret: str,
    code: str,
    now: float | None = None,
    window: int = 1,
    last_counter: int | None = None,
) -> int | None:
    """Return the matching counter when ``code`` is valid, else None.

    Accepts ``window`` steps of clock drift either way and refuses counters that were
    already used (``last_counter``) so an intercepted code cannot be replayed.
    """
    cleaned = code.strip().replace(" ", "")
    if not _CODE_RE.fullmatch(cleaned):
        return None
    base = current_counter(now)
    for offset in range(-window, window + 1):
        counter = base + offset
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(code_at(secret, counter).encode(), cleaned.encode()):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Crypie") -> str:
    """The otpauth:// URI that authenticator apps read from a QR code."""
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def pretty_secret(secret: str) -> str:
    """Secret in groups of four, for typing it by hand."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
