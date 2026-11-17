"""Everything the login routes need: account, second factor, recovery codes, throttling."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.passwords import hash_password, verify_password
from app.auth.session import (
    KNOWN_DEVICE,
    LOGIN,
    MFA_SETUP,
    REAUTH,
    SESSION,
    SETUP_RESUME,
    SessionData,
    SessionSigner,
)
from app.config import Settings
from app.db import AuthThrottle, AuthUser, Setting, utcnow

log = logging.getLogger("crypie.auth")

RECOVERY_CODE_COUNT = 8
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i to avoid misreading
STAGE_ONE_SECONDS = 10 * 60  # time allowed between password and second factor
MFA_SETUP_SECONDS = 15 * 60  # time allowed to scan the QR code and confirm a code
REAUTH_SECONDS = 5 * 60  # time allowed between re-entering password + code and re-linking
ENROL_TOKEN_SECONDS = 60 * 60  # lifetime of the one-time enrol code printed by the CLI
# How long after creating the account at /setup the setup link plus the password may restart the
# linking of the first authenticator, and only in the browser that made the account (it holds a
# signed cookie). Later, or elsewhere, only the enrol code from the CLI works (shell access).
SETUP_RESUME_SECONDS = 60 * 60
# A browser that completed a login (password plus second factor) is remembered this long; it is
# never held up by the account-wide password limit below, only by the one for its own address.
KNOWN_DEVICE_SECONDS = 180 * 24 * 3600
# Account-wide limit on wrong passwords from browsers that never logged in: many addresses
# (a botnet, a cheap IPv6 /48) together get this many attempts before they share a wait.
PASSWORD_ACCOUNT_MAX_FAILURES = 20
# Throttle keys that hold for the whole account; never evicted early (see _make_room).
ACCOUNT_KEY_PREFIXES = ("mfa:", "pw:")
MIN_PASSWORD_LENGTH = 10
MIN_SESSION_SECRET_LENGTH = 32
SESSION_SECRET_KEY = "auth:session_secret"
RECOVERY_HASH_PREFIX = "h1$"


def _normalise_code(code: str) -> str:
    return code.strip().lower().replace("-", "").replace(" ", "")


def _legacy_hash_code(code: str) -> str:
    """Unkeyed SHA-256, as stored before keyed hashes; still accepted for existing codes."""
    return sha256(_normalise_code(code).encode()).hexdigest()


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


def session_secret_is_weak(settings: Settings) -> bool:
    """True when SESSION_SECRET is unset (the key then lives in the database) or too short."""
    return len(settings.session_secret) < MIN_SESSION_SECRET_LENGTH


def validate_new_password(password: str, repeat: str) -> str | None:
    """Return a Dutch error message, or None when the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Kies een wachtwoord van minstens {MIN_PASSWORD_LENGTH} tekens."
    if password != repeat:
        return "De twee wachtwoorden zijn niet gelijk."
    return None


@dataclass
class LoginThrottle:
    """Slows down password and code guessing per key (client address or account).

    Kept in the database so a restart does not hand out a fresh set of attempts. Failures are
    forgotten after ``decay_seconds`` without a new failure.
    """

    max_failures: int = 5
    base_block_seconds: int = 30
    max_block_seconds: int = 15 * 60
    decay_seconds: int = 24 * 3600
    max_entries: int = 10_000

    def _row(self, db: Session, key: str, now: float) -> AuthThrottle | None:
        row = db.get(AuthThrottle, key[:128])
        if row is not None and now - row.last_failure > self.decay_seconds:
            return None
        return row

    def blocked_for(self, db: Session, key: str, now: float | None = None) -> int:
        """Seconds the client still has to wait, 0 when it may try."""
        now = time.time() if now is None else now
        row = self._row(db, key, now)
        if row is None or row.failures < self.max_failures:
            return 0
        return max(0, int(row.blocked_until - now))

    def failures(self, db: Session, key: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        row = self._row(db, key, now)
        return row.failures if row is not None else 0

    def record_failure(self, db: Session, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        try:
            self._record(db, key[:128], now)
        except IntegrityError:  # a parallel request created the row first
            db.rollback()
            self._record(db, key[:128], now)

    def _record(self, db: Session, key: str, now: float) -> None:
        row = db.get(AuthThrottle, key)
        if row is None:
            self._make_room(db, now)
            row = AuthThrottle(key=key, failures=0, blocked_until=0.0, last_failure=now)
            db.add(row)
        elif now - row.last_failure > self.decay_seconds:
            row.failures = 0
        row.failures += 1
        row.last_failure = now
        block = 0.0
        if row.failures >= self.max_failures:
            block = min(
                self.base_block_seconds * 2 ** min(row.failures - self.max_failures, 30),
                self.max_block_seconds,
            )
        row.blocked_until = now + block
        db.commit()

    def _make_room(self, db: Session, now: float) -> None:
        """Never grow without bound: drop stale entries, then the oldest per-address ones.

        Per-account rows (``mfa:``, ``pw:``) are never evicted early: otherwise anyone could
        reset an account-wide counter by filling the table from many addresses.
        """
        count = db.scalar(select(func.count()).select_from(AuthThrottle)) or 0
        if count < self.max_entries:
            return
        db.execute(delete(AuthThrottle).where(AuthThrottle.last_failure < now - self.decay_seconds))
        count = db.scalar(select(func.count()).select_from(AuthThrottle)) or 0
        excess = count - self.max_entries + 1
        if excess > 0:
            oldest = db.scalars(
                select(AuthThrottle.key)
                .where(*(AuthThrottle.key.not_like(f"{p}%") for p in ACCOUNT_KEY_PREFIXES))
                .order_by(AuthThrottle.last_failure)
                .limit(excess)
            ).all()
            db.execute(delete(AuthThrottle).where(AuthThrottle.key.in_(oldest)))

    def reset(self, db: Session, key: str) -> None:
        db.execute(delete(AuthThrottle).where(AuthThrottle.key == key[:128]))
        db.commit()

    def clear(self, db: Session) -> int:
        removed = db.execute(delete(AuthThrottle)).rowcount or 0
        db.commit()
        return removed


class AuthService:
    """One account, stored in ``auth_users``; created at /setup or with the CLI."""

    def __init__(self, settings: Settings, secret: str):
        self.session_seconds = settings.auth_session_hours * 3600
        self.signer = SessionSigner(
            secret,
            {
                SESSION: self.session_seconds,
                LOGIN: STAGE_ONE_SECONDS,
                MFA_SETUP: MFA_SETUP_SECONDS,
                REAUTH: REAUTH_SECONDS,
                SETUP_RESUME: SETUP_RESUME_SECONDS,
                KNOWN_DEVICE: KNOWN_DEVICE_SECONDS,
            },
        )
        self._code_key = hmac.new(secret.encode(), b"crypie recovery codes", sha256).digest()
        self._enrol_key = hmac.new(secret.encode(), b"crypie enrol token", sha256).digest()
        # Password stage: per client address, plus an account-wide limit that only counts and
        # holds up browsers that never completed a login (a known browser is exempt, so no one
        # can lock the owner out). Second-factor stage: per address and per account.
        self.throttle = LoginThrottle()
        self.password_throttle = LoginThrottle(max_failures=PASSWORD_ACCOUNT_MAX_FAILURES)
        # Proof of ownership for /setup while there is no account (see build_auth).
        self.setup_token: str = settings.setup_token or secrets.token_urlsafe(24)
        self.setup_token_from_env = bool(settings.setup_token)
        self._setup_hint_at: float | None = None

    def log_setup_hint(self) -> None:
        """Tell the owner (in the server log, at most every 5 minutes) how to create the
        account; the web pages never reveal that there is none yet."""
        now = time.monotonic()
        if self._setup_hint_at is not None and now - self._setup_hint_at < 300:
            return
        self._setup_hint_at = now
        if self.setup_token_from_env:
            log.warning(
                "no account yet: open /setup?token=<SETUP_TOKEN> to create it, "
                "or run: python -m app.auth create-account"
            )
        else:
            log.warning(
                "no account yet: open /setup?token=%s to create it, "
                "or run: python -m app.auth create-account",
                self.setup_token,
            )

    # -- account ----------------------------------------------------------------------------

    def account(self, session: Session) -> AuthUser | None:
        return session.scalars(select(AuthUser).limit(1)).first()

    def has_account(self, session: Session) -> bool:
        return self.account(session) is not None

    def create_account(
        self, session: Session, username: str, password: str = "", *, password_hash: str = ""
    ) -> AuthUser:
        """``password_hash`` lets the caller hash off the event loop; else ``password`` is
        hashed here."""
        if self.has_account(session):
            raise ValueError("Er is al een account.")
        user = AuthUser(
            username=username.strip(),
            password_hash=password_hash or hash_password(password),
            account_id=secrets.token_urlsafe(16),
            session_version=0,
        )
        session.add(user)
        session.commit()
        return user

    def ensure_account_id(self, session: Session) -> None:
        """Give an account from before account ids existed its own random id."""
        user = self.account(session)
        if user is not None and not user.account_id:
            user.account_id = secrets.token_urlsafe(16)
            session.commit()

    def username_matches(self, user: AuthUser, username: str) -> bool:
        return secrets.compare_digest(username.strip().encode(), user.username.encode())

    def check_password(self, session: Session, username: str, password: str) -> AuthUser | None:
        """The account when username and password match, else None (constant-time compare)."""
        user = self.account(session)
        if user is None:
            return None
        user_ok = self.username_matches(user, username)
        password_ok = verify_password(password, user.password_hash)
        return user if user_ok and password_ok else None

    def change_password(self, session: Session, password: str = "", *, password_hash: str = ""):
        """New password; every existing session ends."""
        user = self.user(session)
        user.password_hash = password_hash or hash_password(password)
        self._bump(user)
        session.commit()

    def store_rehash(self, session: Session, user: AuthUser, password_hash: str) -> None:
        """Upgrade an older hash after a successful login; sessions stay valid."""
        user.password_hash = password_hash
        session.commit()

    def user(self, session: Session) -> AuthUser:
        user = self.account(session)
        if user is None:
            raise ValueError("Er is nog geen account.")
        return user

    # -- sessions ---------------------------------------------------------------------------

    @staticmethod
    def _bump(user: AuthUser) -> None:
        user.session_version = (user.session_version or 0) + 1

    def revoke_sessions(self, session: Session) -> None:
        """Log out every browser: cookies carry the version and stop fitting."""
        self._bump(self.user(session))
        session.commit()

    @staticmethod
    def session_valid(user: AuthUser | None, data: SessionData | None) -> bool:
        """The cookie belongs to this account and was issued after its last revocation."""
        if user is None or data is None or not user.account_id:
            return False
        return (
            secrets.compare_digest(data.username.encode(), user.username.encode())
            and secrets.compare_digest(data.account_id.encode(), user.account_id.encode())
            and data.version == (user.session_version or 0)
        )

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

    def _hash_code(self, code: str) -> str:
        digest = hmac.new(self._code_key, _normalise_code(code).encode(), sha256).hexdigest()
        return RECOVERY_HASH_PREFIX + digest

    def use_recovery_code(self, session: Session, code: str) -> bool:
        """Consume a recovery code; each one works exactly once."""
        user = self.user(session)
        try:
            hashes = json.loads(user.recovery_codes or "[]")
        except ValueError:
            log.warning("stored recovery codes are unreadable; none accepted")
            return False
        if not isinstance(hashes, list):
            return False
        keyed, legacy = self._hash_code(code).encode(), _legacy_hash_code(code).encode()
        match = None
        for stored in hashes:
            wanted = keyed if str(stored).startswith(RECOVERY_HASH_PREFIX) else legacy
            if secrets.compare_digest(str(stored).encode(), wanted) and match is None:
                match = stored
        if match is None:
            return False
        hashes.remove(match)
        user.recovery_codes = json.dumps(hashes)
        user.last_login_at = utcnow()
        session.commit()
        return True

    def check_second_factor(self, session: Session, code: str) -> str | None:
        """'totp' or 'recovery' when ``code`` is a valid second factor (a recovery code is used
        up), else None."""
        cleaned = code.strip()
        if not cleaned or len(cleaned) > 32:
            return None
        if all(c in "0123456789 " for c in cleaned):
            return "totp" if self.check_totp(session, cleaned) else None
        return "recovery" if self.use_recovery_code(session, cleaned) else None

    def note_login_failures(self, session: Session, failures: int) -> None:
        """Remember how many wrong codes for the account preceded this successful login."""
        user = self.user(session)
        user.mfa_failures_before_login = max(int(failures), 0)
        session.commit()

    def recovery_codes_left(self, session: Session) -> int:
        try:
            return len(json.loads(self.user(session).recovery_codes or "[]"))
        except (ValueError, TypeError):
            return 0

    def enrol_totp(self, session: Session, secret: str, code: str) -> list[str] | None:
        """Activate ``secret`` once the user proves the app shows the right code.

        Returns the fresh recovery codes (plain text, shown once) or None when the code is wrong.
        Every existing session ends.
        """
        if not totp.SECRET_RE.fullmatch(secret):
            return None
        counter = totp.verify(secret, code)
        if counter is None:
            return None
        user = self.user(session)
        user.totp_secret = secret
        user.totp_enrolled_at = utcnow()
        user.totp_last_counter = counter
        user.last_login_at = utcnow()
        user.enrol_token_hash = None
        user.enrol_token_expires_at = None
        return self.issue_recovery_codes(session, user)

    def issue_recovery_codes(self, session: Session, user: AuthUser | None = None) -> list[str]:
        """New recovery codes; the old ones and every existing session stop working."""
        user = user or self.user(session)
        codes = generate_recovery_codes()
        user.recovery_codes = json.dumps([self._hash_code(c) for c in codes])
        self._bump(user)
        session.commit()
        return codes

    def reset_totp(self, session: Session) -> None:
        user = self.user(session)
        user.totp_secret = None
        user.totp_enrolled_at = None
        user.totp_last_counter = None
        user.recovery_codes = None
        self._bump(user)
        session.commit()

    # -- one-time enrol code (CLI) ----------------------------------------------------------

    def _hash_enrol_token(self, token: str) -> str:
        return hmac.new(self._enrol_key, token.strip().encode(), sha256).hexdigest()

    def issue_enrol_token(self, session: Session) -> str:
        """A one-time code that allows linking an authenticator within the next hour."""
        user = self.user(session)
        token = secrets.token_urlsafe(12)
        user.enrol_token_hash = self._hash_enrol_token(token)
        user.enrol_token_expires_at = utcnow() + timedelta(seconds=ENROL_TOKEN_SECONDS)
        session.commit()
        return token

    def check_enrol_token(self, session: Session, token: str) -> bool:
        user = self.user(session)
        expires = user.enrol_token_expires_at
        if not user.enrol_token_hash or expires is None or not token.strip():
            return False
        if expires.tzinfo is None:  # SQLite hands back naive datetimes
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return False
        try:
            wanted = self._hash_enrol_token(token).encode()
        except UnicodeEncodeError:
            return False
        return secrets.compare_digest(user.enrol_token_hash.encode(), wanted)

    # -- cookies ---------------------------------------------------------------------------

    def stage_one_token(self, user: AuthUser, enrol: bool = False) -> str:
        """Password checked; ``enrol`` = allowed to link an authenticator (setup or CLI code)."""
        return self.signer.encode(
            user.username,
            mfa=False,
            max_age=STAGE_ONE_SECONDS,
            purpose=LOGIN,
            account_id=user.account_id,
            version=user.session_version or 0,
            extra={"enrol": True} if enrol else None,
        )

    def full_token(self, user: AuthUser, expires_at: int | None = None) -> str:
        """A complete session; ``expires_at`` re-issues it without extending its lifetime."""
        return self.signer.encode(
            user.username,
            mfa=True,
            max_age=self.session_seconds,
            purpose=SESSION,
            account_id=user.account_id,
            version=user.session_version or 0,
            expires_at=expires_at,
        )

    def reauth_token(self, user: AuthUser) -> str:
        return self.signer.encode(
            user.username,
            mfa=True,
            max_age=REAUTH_SECONDS,
            purpose=REAUTH,
            account_id=user.account_id,
            version=user.session_version or 0,
        )

    def setup_resume_token(self, user: AuthUser) -> str:
        """Given to the browser that made the account at /setup: with it, and only with it, the
        setup link plus the password may restart the linking of the first authenticator."""
        return self.signer.encode(
            user.username,
            mfa=False,
            max_age=SETUP_RESUME_SECONDS,
            purpose=SETUP_RESUME,
            account_id=user.account_id,
            version=user.session_version or 0,
        )

    def setup_resume_allowed(self, user: AuthUser, token: str | None) -> bool:
        """True when ``token`` is this account's setup-resume cookie (see setup_resume_token)."""
        data = self.signer.decode(token, purpose=SETUP_RESUME)
        return (
            data is not None
            and data.account_id == user.account_id
            and data.version == (user.session_version or 0)
            and self.username_matches(user, data.username)
        )

    def known_device_token(self, user: AuthUser) -> str:
        """Given after a complete login: this browser is exempt from the account-wide password
        limit. Not tied to the session version, so logging out (everywhere) keeps it; it grants
        no access by itself."""
        return self.signer.encode(
            user.username,
            mfa=False,
            max_age=KNOWN_DEVICE_SECONDS,
            purpose=KNOWN_DEVICE,
            account_id=user.account_id,
        )

    def known_device(self, user: AuthUser, token: str | None) -> bool:
        """True when ``token`` is this account's known-browser cookie (see known_device_token)."""
        data = self.signer.decode(token, purpose=KNOWN_DEVICE)
        return (
            data is not None
            and bool(user.account_id)
            and secrets.compare_digest(data.account_id.encode(), user.account_id.encode())
        )

    def setup_secret_token(self, user: AuthUser, secret: str) -> str:
        return self.signer.encode(
            user.username,
            mfa=False,
            max_age=MFA_SETUP_SECONDS,
            purpose=MFA_SETUP,
            account_id=user.account_id,
            version=user.session_version or 0,
            extra={"secret": secret},
        )

    def setup_secret(self, user: AuthUser, token: str | None) -> str | None:
        """The candidate authenticator secret from the enrolment cookie, if it is ours."""
        data = self.signer.decode(token, purpose=MFA_SETUP)
        if not self.session_valid(user, data):
            return None
        secret = str(data.extra.get("secret", ""))
        return secret if totp.SECRET_RE.fullmatch(secret) else None
