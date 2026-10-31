"""Login with password and authenticator code, enrolment, recovery codes and route protection."""

import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import totp
from app.auth.passwords import hash_password, is_hash, verify_password
from app.auth.service import LoginThrottle
from app.auth.session import COOKIE_NAME, SessionSigner
from app.config import get_settings
from app.db import AuthUser, SessionLocal
from app.main import app

AUTH_ENV = {
    "AUTH_ENABLED": "true",
    "AUTH_USERNAME": "kris",
    "AUTH_PASSWORD": "geheim-wachtwoord",
    "SESSION_SECRET": "test-secret",
}


# -- units ---------------------------------------------------------------------------------


def test_totp_matches_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.verify(secret, "287 082", now=59) == 1
    assert totp.verify(secret, "287082", now=59 + 30) == 1  # one step of drift is fine
    assert totp.verify(secret, "287082", now=59 + 90) is None
    assert totp.verify(secret, "287082", now=59, last_counter=1) is None  # no replay
    assert totp.verify(secret, "12345", now=59) is None
    uri = totp.provisioning_uri(secret, "kris")
    assert uri.startswith("otpauth://totp/Crypie%3Akris?secret=GEZD") and "issuer=Crypie" in uri
    assert totp.pretty_secret("ABCDEFGH") == "ABCD EFGH"


def test_password_hash_and_plain():
    hashed = hash_password("hunter2", iterations=1000)
    assert is_hash(hashed) and verify_password("hunter2", hashed)
    assert not verify_password("hunter3", hashed)
    assert verify_password("plain", "plain") and not verify_password("plain", "other")
    assert not verify_password("x", "") and not verify_password("x", "pbkdf2_sha256$broken")


def test_session_cookie_is_signed_and_expires():
    signer = SessionSigner("s3cret")
    token = signer.encode("kris", mfa=True, max_age=60, now=1000)
    data = signer.decode(token, now=1030)
    assert data is not None and data.username == "kris" and data.complete
    assert signer.decode(token, now=1060) is None
    assert signer.decode(token[:-1] + ("0" if token[-1] != "0" else "1"), now=1030) is None
    assert SessionSigner("other").decode(token, now=1030) is None
    assert signer.decode(None) is None and signer.decode("garbage") is None


def test_throttle_backs_off_after_failures():
    t = LoginThrottle(max_failures=3, base_block_seconds=10, max_block_seconds=25)
    for _ in range(2):
        t.record_failure("ip", now=0)
    assert t.blocked_for("ip", now=0) == 0
    t.record_failure("ip", now=0)
    assert t.blocked_for("ip", now=0) == 10
    t.record_failure("ip", now=0)
    assert t.blocked_for("ip", now=0) == 20
    t.record_failure("ip", now=0)
    assert t.blocked_for("ip", now=0) == 25
    assert t.blocked_for("ip", now=30) == 0
    t.reset("ip")
    assert t.blocked_for("ip", now=0) == 0


# -- the app -----------------------------------------------------------------------------


@pytest.fixture
async def client(request):
    overrides = dict(AUTH_ENV)
    overrides.update(getattr(request, "param", {}))
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        async with app.router.lifespan_context(app):
            with SessionLocal() as db:  # the in-memory database outlives one test
                db.query(AuthUser).delete()
                db.commit()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def _current_code(secret: str) -> str:
    return totp.code_at(secret, totp.current_counter())


def _forget_last_code() -> None:
    """Tests run inside one 30-second step; pretend the previous code was long ago."""
    with SessionLocal() as db:
        for user in db.query(AuthUser).all():
            user.totp_last_counter = None
        db.commit()


async def _enrol(client: AsyncClient) -> tuple[str, list[str]]:
    """Log in with the password and link an authenticator; return (secret, recovery codes)."""
    r = await client.post(
        "/login", data={"username": "kris", "password": "geheim-wachtwoord", "next": "/trading"}
    )
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa/setup?next=/trading"
    r = await client.get("/login/mfa/setup?next=/trading")
    assert r.status_code == 200 and "<svg" in r.text and "Authenticator instellen" in r.text
    secret = app.state.auth.signer.decode(client.cookies["crypie_mfa_setup"]).username
    r = await client.post(
        "/login/mfa/setup", data={"code": _current_code(secret), "next": "/trading"}
    )
    assert r.status_code == 200 and "Herstelcodes" in r.text
    codes = re.findall(r'<li class="mono">([a-z0-9-]+)</li>', r.text)
    assert len(codes) == 8 and all(len(c) == 11 and c[5] == "-" for c in codes)
    return secret, codes


async def test_everything_needs_a_login(client):
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.get("/trading?x=1")
    assert r.headers["location"] == "/login?next=/trading?x=1"
    r = await client.get("/api/prices")
    assert r.status_code == 401 and r.json() == {"detail": "Niet ingelogd"}
    r = await client.get("/docs")
    assert r.status_code == 303
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/static/app.css")).status_code == 200
    r = await client.get("/login")
    assert r.status_code == 200 and "Gebruikersnaam" in r.text
    # Half a login (password only) does not open the dashboard.
    r = await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    assert r.status_code == 303
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/api/balance")).status_code == 401


async def test_wrong_password_is_rejected_and_throttled(client):
    r = await client.post("/login", data={"username": "kris", "password": "fout"})
    assert r.status_code == 401 and "klopt niet" in r.text and COOKIE_NAME not in client.cookies
    r = await client.post("/login", data={"username": "iemand", "password": "geheim-wachtwoord"})
    assert r.status_code == 401
    for _ in range(3):
        await client.post("/login", data={"username": "kris", "password": "fout"})
    r = await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    assert r.status_code == 429 and "Te veel" in r.text


async def test_first_login_enrols_then_codes_and_recovery_work(client):
    secret, codes = await _enrol(client)
    r = await client.get("/trading")
    assert r.status_code == 200 and "Uitloggen" in r.text and 'href="/account"' in r.text
    assert (await client.get("/api/trading/status")).status_code == 200

    r = await client.get("/account")
    assert r.status_code == 200 and "8 van 8" in r.text and "Actief" in r.text

    r = await client.post("/logout")
    assert r.status_code == 303
    assert (await client.get("/")).status_code == 303

    # Second login: password, then the code. A code cannot be used twice.
    r = await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    assert r.headers["location"] == "/login/mfa?next=/"
    r = await client.get("/login/mfa")
    assert r.status_code == 200 and "Verificatiecode" in r.text
    r = await client.post("/login/mfa", data={"code": "000000"})
    assert r.status_code == 401 and "klopt niet" in r.text
    _forget_last_code()
    code = _current_code(secret)
    r = await client.post("/login/mfa", data={"code": code, "next": "/news"})
    assert r.status_code == 303 and r.headers["location"] == "/news"
    assert (await client.get("/news")).status_code == 200
    await client.post("/logout")
    await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    r = await client.post("/login/mfa", data={"code": code})
    assert r.status_code == 401  # replay

    # Recovery code instead of the app, each one once.
    r = await client.post("/login/mfa", data={"code": codes[0].upper()})
    assert r.status_code == 303
    assert "7 van 8" in (await client.get("/account")).text
    await client.post("/logout")
    await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    assert (await client.post("/login/mfa", data={"code": codes[0]})).status_code == 401
    assert (await client.post("/login/mfa", data={"code": codes[1]})).status_code == 303

    # Account actions need the password again.
    r = await client.post("/account", data={"action": "recovery", "password": "fout"})
    assert r.status_code == 401
    r = await client.post("/account", data={"action": "recovery", "password": "geheim-wachtwoord"})
    assert r.status_code == 200 and "Herstelcodes" in r.text
    assert "8 van 8" in (await client.get("/account")).text
    r = await client.post("/account", data={"action": "reenrol", "password": "geheim-wachtwoord"})
    assert r.headers["location"] == "/login/mfa/setup?next=/account"
    r = await client.get("/login/mfa/setup?next=/account")
    assert r.status_code == 200 and "Nieuwe authenticator" in r.text
    new_secret = app.state.auth.signer.decode(client.cookies["crypie_mfa_setup"]).username
    assert new_secret != secret
    r = await client.post("/login/mfa/setup", data={"code": "000000", "next": "/account"})
    assert r.status_code == 400
    r = await client.post(
        "/login/mfa/setup", data={"code": _current_code(new_secret), "next": "/account"}
    )
    assert r.status_code == 200 and "Herstelcodes" in r.text
    await client.post("/logout")
    await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    _forget_last_code()
    r = await client.post("/login/mfa", data={"code": _current_code(secret)})
    assert r.status_code == 401  # the old authenticator no longer works
    r = await client.post("/login/mfa", data={"code": _current_code(new_secret)})
    assert r.status_code == 303


async def test_open_redirects_are_not_followed(client):
    secret, codes = await _enrol(client)
    await client.post("/logout")
    await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    _forget_last_code()
    r = await client.post(
        "/login/mfa", data={"code": _current_code(secret), "next": "https://evil.example/"}
    )
    assert r.headers["location"] == "/"
    await client.post("/logout")
    await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    # The same code was just used, so log in with a recovery code instead.
    r = await client.post("/login/mfa", data={"code": codes[0], "next": "//evil.example/"})
    assert r.status_code == 303 and r.headers["location"] == "/"


async def test_session_cookie_cannot_be_forged(client):
    client.cookies.set(COOKIE_NAME, SessionSigner("wrong").encode("kris", True, 3600))
    assert (await client.get("/")).status_code == 303
    client.cookies.set(COOKIE_NAME, app.state.auth.signer.encode("kris", False, 3600))
    assert (await client.get("/")).status_code == 303
    client.cookies.set(COOKIE_NAME, app.state.auth.signer.encode("kris", True, 3600))
    assert (await client.get("/")).status_code == 200


@pytest.mark.parametrize("client", [{"AUTH_PASSWORD": "", "AUTH_PASSWORD_HASH": ""}], indirect=True)
async def test_without_password_the_app_stays_closed(client):
    r = await client.get("/")
    assert r.status_code == 503 and "AUTH_PASSWORD" in r.text
    r = await client.post("/login", data={"username": "admin", "password": ""})
    assert r.status_code == 503
    assert (await client.get("/health")).status_code == 200


@pytest.mark.parametrize(
    "client",
    [{"AUTH_PASSWORD": "", "AUTH_PASSWORD_HASH": hash_password("geheim-wachtwoord", 1000)}],
    indirect=True,
)
async def test_password_hash_from_env(client):
    r = await client.post("/login", data={"username": "kris", "password": "geheim-wachtwoord"})
    assert r.status_code == 303
    r = await client.post("/login", data={"username": "kris", "password": "anders"})
    assert r.status_code == 401


def test_cli_hash_and_secret(capsys):
    from app.auth.__main__ import main

    assert main(["hash-password", "abc"]) == 0
    out = capsys.readouterr().out.strip()
    assert is_hash(out) and verify_password("abc", out)
    assert main(["secret"]) == 0
    assert len(capsys.readouterr().out.strip()) >= 40
