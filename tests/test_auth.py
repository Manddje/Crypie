"""Account setup, login with password and authenticator code, recovery codes, route protection."""

import io
import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import totp
from app.auth.__main__ import main as auth_cli
from app.auth.passwords import MIN_ITERATIONS, hash_password, is_hash, verify_password
from app.auth.service import LoginThrottle, load_or_create_session_secret
from app.auth.session import COOKIE_NAME, LOGIN, MFA_SETUP, SessionSigner
from app.config import get_settings
from app.db import AuthThrottle, AuthUser, SessionLocal, Setting, init_db
from app.main import app

AUTH_ENV = {"AUTH_ENABLED": "true", "SESSION_SECRET": "", "SETUP_TOKEN": ""}
PASSWORD = "geheim-wachtwoord"


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


def test_password_hash():
    hashed = hash_password("hunter2", iterations=MIN_ITERATIONS)
    assert is_hash(hashed) and verify_password("hunter2", hashed)
    assert not verify_password("hunter3", hashed)
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
    init_db()
    t = LoginThrottle(max_failures=3, base_block_seconds=10, max_block_seconds=25)
    with SessionLocal() as db:
        t.reset(db, "unit-ip")
        for _ in range(2):
            t.record_failure(db, "unit-ip", now=0)
        assert t.blocked_for(db, "unit-ip", now=0) == 0
        t.record_failure(db, "unit-ip", now=0)
        assert t.blocked_for(db, "unit-ip", now=0) == 10
        t.record_failure(db, "unit-ip", now=0)
        assert t.blocked_for(db, "unit-ip", now=0) == 20
        t.record_failure(db, "unit-ip", now=0)
        assert t.blocked_for(db, "unit-ip", now=0) == 25
        assert t.blocked_for(db, "unit-ip", now=30) == 0
        t.reset(db, "unit-ip")
        assert t.blocked_for(db, "unit-ip", now=0) == 0


def test_session_secret_is_generated_once_and_kept():
    init_db()
    with SessionLocal() as db:
        db.query(Setting).filter(Setting.key == "auth:session_secret").delete()
        db.commit()
        first = load_or_create_session_secret(db)
        assert len(first) >= 40 and load_or_create_session_secret(db) == first


# -- the app -----------------------------------------------------------------------------


def _clear_auth_tables() -> None:
    with SessionLocal() as db:  # the in-memory database outlives one test
        db.query(AuthUser).delete()
        db.query(AuthThrottle).delete()
        db.commit()


@pytest.fixture
async def client(request):
    overrides = dict(AUTH_ENV)
    overrides.update(getattr(request, "param", {}))
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        async with app.router.lifespan_context(app):
            _clear_auth_tables()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
            _clear_auth_tables()
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


def _candidate_secret(client: AsyncClient) -> str:
    data = app.state.auth.signer.decode(client.cookies["crypie_mfa_setup"], purpose=MFA_SETUP)
    return data.extra["secret"]


def _recovery_codes(html: str) -> list[str]:
    return re.findall(r'<li class="mono">([a-z0-9-]+)</li>', html)


def _setup_token() -> str:
    return app.state.auth.setup_token


async def _setup(client: AsyncClient) -> tuple[str, list[str]]:
    """Create the account at /setup and link an authenticator; return (secret, recovery codes)."""
    r = await client.post(
        "/setup",
        data={
            "username": "kris",
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "token": _setup_token(),
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa/setup"
    r = await client.get("/login/mfa/setup")
    assert r.status_code == 200 and "<svg" in r.text and "Authenticator instellen" in r.text
    secret = _candidate_secret(client)
    r = await client.post("/login/mfa/setup", data={"code": _current_code(secret)})
    assert r.status_code == 200 and "Herstelcodes" in r.text
    codes = _recovery_codes(r.text)
    assert len(codes) == 8 and all(len(c) == 11 and c[5] == "-" for c in codes)
    return secret, codes


async def _login(client: AsyncClient, password: str = PASSWORD):
    return await client.post("/login", data={"username": "kris", "password": password})


async def _full_login(client: AsyncClient, secret: str, password: str = PASSWORD):
    await client.post("/logout")
    assert (await _login(client, password)).status_code == 303
    _forget_last_code()
    r = await client.post("/login/mfa", data={"code": _current_code(secret)})
    assert r.status_code == 303
    return r


async def test_without_an_account_nothing_gives_away_that_setup_is_open(client):
    for path in ("/", "/trading", "/account", "/docs"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    r = await client.get("/login")
    assert r.status_code == 200 and "Gebruikersnaam" in r.text
    r = await client.get("/api/prices")
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"
    # RFC 9110: a 401 names its challenge; the metadata it names is one that is served.
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert '/.well-known/oauth-protected-resource"' in challenge
    assert (await client.get("/health")).status_code == 200
    # /setup only with the token from the server log.
    r = await client.get("/setup")
    assert r.status_code == 404 and "setup-link" in r.text and "Gebruikersnaam" not in r.text
    r = await client.get("/setup?token=fout")
    assert r.status_code == 403 and "klopt niet" in r.text
    r = await client.post(
        "/setup", data={"username": "kris", "password": PASSWORD, "password_repeat": PASSWORD}
    )
    assert r.status_code == 404
    with SessionLocal() as db:
        assert db.query(AuthUser).count() == 0
    r = await client.get(f"/setup?token={_setup_token()}")
    assert r.status_code == 200 and "Account aanmaken" in r.text and _setup_token() in r.text
    token = _setup_token()
    r = await client.post(
        "/setup",
        data={"username": "k", "password": PASSWORD, "password_repeat": PASSWORD, "token": token},
    )
    assert r.status_code == 400 and "Gebruikersnaam" in r.text
    r = await client.post(
        "/setup",
        data={"username": "kris", "password": "kort", "password_repeat": "kort", "token": token},
    )
    assert r.status_code == 400 and "minstens 10" in r.text
    r = await client.post(
        "/setup",
        data={"username": "kris", "password": PASSWORD, "password_repeat": "x", "token": token},
    )
    assert r.status_code == 400 and "niet gelijk" in r.text
    await _setup(client)
    # Once the account exists /setup is gone, for everyone.
    await client.post("/logout")
    r = await client.get(f"/setup?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.post(
        "/setup",
        data={"username": "x", "password": PASSWORD, "password_repeat": PASSWORD, "token": token},
    )
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_everything_needs_a_login(client):
    await _setup(client)
    await client.post("/logout")
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.get("/trading?x=1")
    assert r.headers["location"] == "/login?next=/trading?x=1"
    r = await client.get("/api/prices")
    assert r.status_code == 401 and r.json()["code"] == "not_authenticated"
    assert (await client.get("/docs")).status_code == 303
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/static/app.css")).status_code == 200
    r = await client.get("/login")
    assert r.status_code == 200 and "Gebruikersnaam" in r.text
    # Half a login (password only) does not open the dashboard.
    assert (await _login(client)).status_code == 303
    assert (await client.get("/")).status_code == 303
    assert (await client.get("/api/balance")).status_code == 401


async def test_wrong_password_is_rejected_and_throttled(client):
    await _setup(client)
    await client.post("/logout")
    r = await _login(client, "fout")
    assert r.status_code == 401 and "klopt niet" in r.text and COOKIE_NAME not in client.cookies
    r = await client.post("/login", data={"username": "iemand", "password": PASSWORD})
    assert r.status_code == 401
    for _ in range(3):
        await _login(client, "fout")
    r = await _login(client)
    assert r.status_code == 429 and "Te veel" in r.text


async def test_login_with_code_recovery_and_account_actions(client):
    secret, codes = await _setup(client)
    r = await client.get("/trading")
    assert r.status_code == 200 and "Uitloggen" in r.text and 'href="/account"' in r.text
    assert (await client.get("/api/trading/status")).status_code == 200
    r = await client.get("/account")
    assert r.status_code == 200 and "8 van 8" in r.text and "Actief" in r.text

    r = await client.post("/logout")
    assert r.status_code == 303
    assert (await client.get("/")).status_code == 303

    # Second login: password, then the code. A code cannot be used twice.
    r = await client.post("/login", data={"username": "kris", "password": PASSWORD, "next": "/x"})
    assert r.headers["location"] == "/login/mfa?next=/x"
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
    await _login(client)
    assert (await client.post("/login/mfa", data={"code": code})).status_code == 401  # replay

    # Recovery code instead of the app, each one once.
    r = await client.post("/login/mfa", data={"code": codes[0].upper()})
    assert r.status_code == 303
    assert "7 van 8" in (await client.get("/account")).text
    await client.post("/logout")
    await _login(client)
    assert (await client.post("/login/mfa", data={"code": codes[0]})).status_code == 401
    assert (await client.post("/login/mfa", data={"code": codes[1]})).status_code == 303

    # Account actions need the password again; new recovery codes also a current code.
    r = await client.post("/account", data={"action": "recovery", "password": "fout"})
    assert r.status_code == 401
    r = await client.post("/account", data={"action": "recovery", "password": PASSWORD})
    assert r.status_code == 401 and "code klopt niet" in r.text
    r = await client.post(
        "/account", data={"action": "recovery", "password": PASSWORD, "code": codes[2]}
    )
    assert r.status_code == 200 and len(_recovery_codes(r.text)) == 8
    assert "8 van 8" in (await client.get("/account")).text  # this browser stays logged in

    # Change the password.
    r = await client.post(
        "/account",
        data={
            "action": "password",
            "password": PASSWORD,
            "new_password": "kort",
            "new_password_repeat": "kort",
        },
    )
    assert r.status_code == 400 and "minstens 10" in r.text
    r = await client.post(
        "/account",
        data={
            "action": "password",
            "password": PASSWORD,
            "new_password": "nieuw-wachtwoord",
            "new_password_repeat": "nieuw-wachtwoord",
        },
    )
    assert r.status_code == 200 and "gewijzigd" in r.text
    assert (await client.get("/account")).status_code == 200
    await client.post("/logout")
    assert (await _login(client)).status_code == 401
    assert (await _login(client, "nieuw-wachtwoord")).status_code == 303

    # Link a new authenticator: password plus a current code; the old one stops working.
    _forget_last_code()
    await client.post("/login/mfa", data={"code": _current_code(secret)})
    r = await client.post("/account", data={"action": "reenrol", "password": "nieuw-wachtwoord"})
    assert r.status_code == 401 and "code klopt niet" in r.text
    _forget_last_code()
    r = await client.post(
        "/account",
        data={
            "action": "reenrol",
            "password": "nieuw-wachtwoord",
            "code": _current_code(secret),
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/account/mfa"
    r = await client.get("/account/mfa")
    assert r.status_code == 200 and "Nieuwe authenticator" in r.text
    new_secret = _candidate_secret(client)
    assert new_secret != secret
    r = await client.post("/account/mfa", data={"code": "000000"})
    assert r.status_code == 400
    r = await client.post("/account/mfa", data={"code": _current_code(new_secret)})
    assert r.status_code == 200 and "Herstelcodes" in r.text
    assert (await client.get("/account")).status_code == 200
    await client.post("/logout")
    await _login(client, "nieuw-wachtwoord")
    _forget_last_code()
    assert (
        await client.post("/login/mfa", data={"code": _current_code(secret)})
    ).status_code == 401
    r = await client.post("/login/mfa", data={"code": _current_code(new_secret)})
    assert r.status_code == 303


async def test_open_redirects_are_not_followed(client):
    secret, codes = await _setup(client)
    await client.post("/logout")
    await _login(client)
    _forget_last_code()
    r = await client.post(
        "/login/mfa", data={"code": _current_code(secret), "next": "https://evil.example/"}
    )
    assert r.headers["location"] == "/"
    await client.post("/logout")
    await _login(client)
    r = await client.post("/login/mfa", data={"code": codes[0], "next": "//evil.example/"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def _token(username: str = "kris", mfa: bool = True, **kw) -> str:
    with SessionLocal() as db:
        user = db.query(AuthUser).one()
        kw.setdefault("account_id", user.account_id)
        kw.setdefault("version", user.session_version)
    return app.state.auth.signer.encode(username, mfa, 3600, **kw)


async def test_session_cookie_cannot_be_forged(client):
    await _setup(client)
    await client.post("/logout")
    client.cookies.set(COOKIE_NAME, SessionSigner("wrong").encode("kris", True, 3600))
    assert (await client.get("/")).status_code == 303
    client.cookies.set(COOKIE_NAME, _token(purpose=LOGIN, mfa=False))
    assert (await client.get("/")).status_code == 303
    client.cookies.set(COOKIE_NAME, _token("iemand-anders"))
    assert (await client.get("/")).status_code == 303
    client.cookies.set(COOKIE_NAME, _token())
    assert (await client.get("/")).status_code == 200


async def test_cli_resets(client, capsys, monkeypatch):
    assert auth_cli(["reset-mfa"]) == 1  # no account yet
    secret, _ = await _setup(client)
    assert (await client.get("/account")).status_code == 200
    monkeypatch.setattr("sys.stdin", io.StringIO("kort\n"))
    assert auth_cli(["reset-password"]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("via-de-server\n"))
    assert auth_cli(["reset-password"]) == 0
    assert (await client.get("/account")).status_code == 303  # every browser is logged out
    assert (await _login(client)).status_code == 401
    r = await _login(client, "via-de-server")
    assert r.headers["location"] == "/login/mfa?next=/"
    assert auth_cli(["reset-mfa"]) == 0
    r = await _login(client, "via-de-server")
    assert r.headers["location"] == "/login/mfa/setup?next=/"
    assert auth_cli(["delete-account", "--yes"]) == 0
    assert (await client.get("/")).headers["location"] == "/login"
    assert "gewijzigd" in capsys.readouterr().out
