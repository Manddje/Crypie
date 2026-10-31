"""Signed, expiring session cookies (HMAC-SHA256, stdlib only)."""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256

COOKIE_NAME = "crypie_session"


@dataclass(frozen=True)
class SessionData:
    username: str
    mfa: bool  # True once the second factor was verified
    issued_at: int
    expires_at: int

    @property
    def complete(self) -> bool:
        return self.mfa


class SessionSigner:
    def __init__(self, secret: str):
        if not secret:
            raise ValueError("session secret must not be empty")
        self._key = sha256(secret.encode()).digest()

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, sha256).hexdigest()

    def encode(self, username: str, mfa: bool, max_age: int, now: float | None = None) -> str:
        now_i = int(time.time() if now is None else now)
        data = {"u": username, "m": mfa, "i": now_i, "e": now_i + max_age}
        payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        return payload.decode().rstrip("=") + "." + self._sign(payload)

    def decode(self, token: str | None, now: float | None = None) -> SessionData | None:
        if not token or "." not in token:
            return None
        body, _, signature = token.rpartition(".")
        payload = (body + "=" * (-len(body) % 4)).encode()
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
            session = SessionData(
                username=str(data["u"]),
                mfa=bool(data["m"]),
                issued_at=int(data["i"]),
                expires_at=int(data["e"]),
            )
        except (ValueError, KeyError, TypeError):
            return None
        if session.expires_at <= int(time.time() if now is None else now):
            return None
        return session
