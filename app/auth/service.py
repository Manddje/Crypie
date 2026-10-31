"""Everything the login routes need: account, second factor, recovery codes, throttling."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.passwords import hash_password, verify_password
from app.auth.session import SessionSigner
from app.config import Settings
from app.db import AuthUser, Setting, utcnow

RECOVERY_CODE_COUNT = 8
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i to avoid misreading
STAGE_ONE_SECONDS = 10 * 60  # time allowed between password and second factor
MIN_PASSWORD_LENGTH = 10
SESSION_SECRET_KEY = "auth:session_secret"


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


def load_or_create_session_secret(session: Session) -> str:
    """The cookie-signing secret, generated once and kept in the settings table."""
    row = session.get(Setting, SESSION_SECRET_KEY)
    if row is None:
        row = Setting(key=SESSION_SECRET_KEY, value=secrets.token_urlsafe(48))
        session.add(row)
        session.commit()
    return row.value


def validate_new_password(password: str, repeat: str) -> str | None:
    """Return a Dutch error message, or None when the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Kies een wachtwoord van minstens {MIN_PASSWORD_LENGTH} tekens."
    if password != repeat:
        return "De twee wachtwoorden zijn niet gelijk."
    return None


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
    """One account, stored in ``auth_users``; created at /setup, never from the environment."""

    def __init__(self, settings: Settings, secret: str):
        self.signer = SessionSigner(secret)
        self.session_seconds = settings.auth_session_hours * 3600
        self.throttle = LoginThrottle()

    # -- account ----------------------------------------------------------------------------

    def account(self, session: Session) -> AuthUser | None:
        return session.scalars(select(AuthUser).limit(1)).first()

    def has_account(self, session: Session) -> bool:
        return self.account(session) is not None

    def create_account(self, session: Session, username: str, password: str) -> AuthUser:
        if self.has_account(session):
            raise ValueError("Er is al een account.")
        user = AuthUser(username=username.strip(), password_hash=hash_password(password))
        session.add(user)
        session.commit()
        return user

    def check_password(self, session: Session, username: str, password: str) -> AuthUser | None:
        """The account when username and password match, else None (constant-time compare)."""
        user = self.account(session)
        if user is None:
            return None
        user_ok = secrets.compare_digest(username.strip().encode(), user.username.encode())
        password_ok = verify_password(password, user.password_hash)
        return user if user_ok and password_ok else None

    def change_password(self, session: Session, password: str) -> None:
        user = self.account(session)
        if user is None:
            raise ValueError("Er is nog geen account.")
        user.password_hash = hash_password(password)
        session.commit()

    def user(self, session: Session) -> AuthUser:
        user = self.account(session)
        if user is None:
            raise ValueError("Er is nog geen account.")
        return user

    # -- second factor ----------------------------------------------------------------------

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

    def stage_one_token(self, username: str) -> str:
        return self.signer.encode(username, mfa=False, max_age=STAGE_ONE_SECONDS)

    def full_token(self, username: str) -> str:
        return self.signer.encode(username, mfa=True, max_age=self.session_seconds)
