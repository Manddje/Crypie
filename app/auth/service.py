"""Everything the login routes need: credentials, second factor, recovery codes, throttling."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256

from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.passwords import verify_password
from app.auth.session import SessionSigner
from app.config import Settings
from app.db import AuthUser, utcnow

RECOVERY_CODE_COUNT = 8
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i to avoid misreading
STAGE_ONE_SECONDS = 10 * 60  # time allowed between password and second factor


def _hash_code(code: str) -> str:
    return sha256(_normalise_code(code).encode()).hexdigest()


def _normalise_code(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


def generate_recovery_codes() -> list[str]:
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


@dataclass
class LoginThrottle:
    """Slows down password and code guessing per client address, in memory."""

    max_failures: int = 5
    base_block_seconds: int = 30
    max_block_seconds: int = 15 * 60
    _state: dict[str, tuple[int, float]] = field(default_factory=dict)

    def blocked_for(self, key: str, now: float | None = None) -> int:
        """Seconds the client still has to wait, 0 when it may try."""
        now = time.time() if now is None else now
        failures, until = self._state.get(key, (0, 0.0))
        return max(0, int(until - now)) if failures >= self.max_failures else 0

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        failures, _ = self._state.get(key, (0, 0.0))
        failures += 1
        block = 0.0
        if failures >= self.max_failures:
            block = min(
                self.base_block_seconds * 2 ** (failures - self.max_failures),
                self.max_block_seconds,
            )
        self._state[key] = (failures, now + block)
        if len(self._state) > 10_000:  # never grow without bound
            self._state = {k: v for k, v in self._state.items() if v[1] > now}

    def reset(self, key: str) -> None:
        self._state.pop(key, None)


class AuthService:
    def __init__(self, settings: Settings, secret: str):
        self.username = settings.auth_username
        self._password = settings.auth_password_hash or settings.auth_password
        self.signer = SessionSigner(secret)
        self.session_seconds = settings.auth_session_hours * 3600
        self.throttle = LoginThrottle()

    @property
    def configured(self) -> bool:
        """False until a password is set; the dashboard then explains what to configure."""
        return bool(self.username and self._password)

    # -- password -------------------------------------------------------------------------

    def check_password(self, username: str, password: str) -> bool:
        if not self.configured:
            return False
        user_ok = secrets.compare_digest(username.strip().encode(), self.username.encode())
        password_ok = verify_password(password, self._password)
        return user_ok and password_ok

    # -- second factor ----------------------------------------------------------------------

    def user(self, session: Session) -> AuthUser:
        row = session.get(AuthUser, self.username)
        if row is None:
            row = AuthUser(username=self.username)
            session.add(row)
            session.commit()
        return row

    def check_totp(self, session: Session, code: str) -> bool:
        user = self.user(session)
        if not user.totp_secret:
            return False
        counter = totp.verify(user.totp_secret, code, last_counter=user.totp_last_counter)
        if counter is None:
            return False
        user.totp_last_counter = counter
        user.last_login_at = utcnow()
        session.commit()
        return True

    def use_recovery_code(self, session: Session, code: str) -> bool:
        """Consume a recovery code; each one works exactly once."""
        user = self.user(session)
        hashes: list[str] = json.loads(user.recovery_codes or "[]")
        wanted = _hash_code(code)
        match = next((h for h in hashes if secrets.compare_digest(h, wanted)), None)
        if match is None:
            return False
        hashes.remove(match)
        user.recovery_codes = json.dumps(hashes)
        user.last_login_at = utcnow()
        session.commit()
        return True

    def recovery_codes_left(self, session: Session) -> int:
        return len(json.loads(self.user(session).recovery_codes or "[]"))

    def enrol_totp(self, session: Session, secret: str, code: str) -> list[str] | None:
        """Activate ``secret`` once the user proves the app shows the right code.

        Returns the fresh recovery codes (plain text, shown once) or None when the code is wrong.
        """
        counter = totp.verify(secret, code)
        if counter is None:
            return None
        user = self.user(session)
        user.totp_secret = secret
        user.totp_enrolled_at = utcnow()
        user.totp_last_counter = counter
        user.last_login_at = utcnow()
        return self.issue_recovery_codes(session, user)

    def issue_recovery_codes(self, session: Session, user: AuthUser | None = None) -> list[str]:
        user = user or self.user(session)
        codes = generate_recovery_codes()
        user.recovery_codes = json.dumps([_hash_code(c) for c in codes])
        session.commit()
        return codes

    def reset_totp(self, session: Session) -> None:
        user = self.user(session)
        user.totp_secret = None
        user.totp_enrolled_at = None
        user.totp_last_counter = None
        user.recovery_codes = None
        session.commit()

    # -- cookies ---------------------------------------------------------------------------

    def stage_one_token(self) -> str:
        return self.signer.encode(self.username, mfa=False, max_age=STAGE_ONE_SECONDS)

    def full_token(self) -> str:
        return self.signer.encode(self.username, mfa=True, max_age=self.session_seconds)
