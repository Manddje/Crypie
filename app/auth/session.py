"""Signed, expiring cookies (HMAC-SHA256, stdlib only).

Every token names its purpose ("session", "login", "mfa_setup", "reauth"); a token made for one
purpose is never accepted for another, and no token may live longer than its purpose allows.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time
from dataclasses import dataclass, field
from hashlib import sha256

COOKIE_NAME = "crypie_session"
HOST_PREFIX = "__Host-"

# Purposes and the longest lifetime (seconds) a token of that purpose may have.
SESSION = "session"  # a complete login: password plus second factor
LOGIN = "login"  # password checked, second factor still to do
MFA_SETUP = "mfa_setup"  # the candidate authenticator secret during enrolment
REAUTH = "reauth"  # password plus code checked again, to re-link the authenticator
DEFAULT_MAX_LIFETIMES = {
    SESSION: 24 * 3600,
    LOGIN: 10 * 60,
    MFA_SETUP: 15 * 60,
    REAUTH: 5 * 60,
}
CLOCK_SKEW_SECONDS = 60


def cookie_name(base: str, secure: bool) -> str:
    """``__Host-`` names over https (Secure, Path=/, no Domain); plain names over http."""
    return HOST_PREFIX + base if secure else base


@dataclass(frozen=True)
class SessionData:
    username: str
    mfa: bool  # True once the second factor was verified
    issued_at: int
    expires_at: int
    purpose: str = SESSION
    account_id: str = ""
    version: int = -1
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def complete(self) -> bool:
        return self.purpose == SESSION and self.mfa


class SessionSigner:
    def __init__(self, secret: str, max_lifetimes: dict[str, int] | None = None):
        if not secret:
            raise ValueError("session secret must not be empty")
        self._key = sha256(secret.encode()).digest()
        self.max_lifetimes = dict(DEFAULT_MAX_LIFETIMES)
        self.max_lifetimes.update(max_lifetimes or {})

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, sha256).hexdigest().encode()

    def encode(
        self,
        username: str,
        mfa: bool,
        max_age: int,
        now: float | None = None,
        *,
        purpose: str = SESSION,
        account_id: str = "",
        version: int = 0,
        expires_at: int | None = None,
        extra: dict | None = None,
    ) -> str:
        """``expires_at`` keeps an existing expiry (re-issuing a cookie never extends it)."""
        now_i = int(time.time() if now is None else now)
        data = {
            "p": purpose,
            "u": username,
            "a": account_id,
            "v": version,
            "m": mfa,
            "i": now_i,
            "e": now_i + max_age if expires_at is None else int(expires_at),
        }
        if extra:
            data["x"] = extra
        payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        return payload.decode().rstrip("=") + "." + self._sign(payload).decode()

    def decode(
        self, token: str | None, now: float | None = None, *, purpose: str = SESSION
    ) -> SessionData | None:
        """The token's data, or None when it is forged, expired, too long-lived or for another
        purpose. Never raises on malformed input."""
        if not token or "." not in token:
            return None
        try:
            raw = token.encode("ascii")
        except UnicodeEncodeError:
            return None
        body, _, signature = raw.rpartition(b".")
        payload = body + b"=" * (-len(body) % 4)
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload))
            extra = data.get("x") or {}
            session = SessionData(
                username=str(data["u"]),
                mfa=bool(data["m"]),
                issued_at=int(data["i"]),
                expires_at=int(data["e"]),
                purpose=str(data.get("p", "")),
                account_id=str(data.get("a", "")),
                version=int(data.get("v", -1)),
                extra=extra if isinstance(extra, dict) else {},
            )
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            return None
        now_i = int(time.time() if now is None else now)
        max_lifetime = self.max_lifetimes.get(purpose)
        if session.purpose != purpose or max_lifetime is None:
            return None
        if session.issued_at > now_i + CLOCK_SKEW_SECONDS:
            return None
        if session.expires_at - session.issued_at > max_lifetime:
            return None
        if session.expires_at <= now_i:
            return None
        return session
