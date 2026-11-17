"""Security fixes for login, sessions, setup and the web layer (audit F01-F17, F22-F27, F52,
F62, F71)."""

import base64
import io
import json
import logging
import os
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import routes, totp
from app.auth import service as routes_service
from app.auth.__main__ import main as auth_cli
from app.auth.passwords import ITERATIONS, MIN_ITERATIONS, hash_password, verify_password
from app.auth.routes import _client_key, _safe_next
from app.auth.service import LoginThrottle, _legacy_hash_code
from app.auth.session import LOGIN, MFA_SETUP, REAUTH, SESSION, SessionSigner
from app.bitvavo.client import BitvavoClient
from app.config import Settings, get_settings
from app.db import AuthThrottle, AuthUser, SessionLocal, utcnow
from app.main import app, live_blocker
from app.security import MAX_BODY_BYTES, PUBLIC_MAX_BODY_BYTES
from tests.test_auth import (
    AUTH_ENV,
    PASSWORD,
    _candidate_secret,
    _clear_auth_tables,
    _current_code,
    _forget_last_code,
    _login,
    _recovery_codes,
    _setup,
    _setup_token,
)


@pytest.fixture
async def web(request):
    """App with login on; ``request.param`` may set env overrides and "base_url"."""
    params = dict(getattr(request, "param", {}))
    base_url = params.pop("base_url", "http://test")
    overrides = {**AUTH_ENV, **params}
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    get_settings.cache_clear()
    try:
        async with app.router.lifespan_context(app):
            _clear_auth_tables()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url=base_url) as c:
                yield c
            _clear_auth_tables()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def _user() -> AuthUser:
    with SessionLocal() as db:
        return db.query(AuthUser).one()


def _session_data(client: AsyncClient, name: str = "crypie_session"):
    return app.state.auth.signer.decode(client.cookies[name], purpose=SESSION)


def _other_browser(client: AsyncClient) -> AsyncClient:
    """A second browser holding a copy of the current login cookie."""
    other = AsyncClient(transport=ASGITransport(app=app), base_url=str(client.base_url))
    for cookie in client.cookies.jar:
        other.cookies.set(cookie.name, cookie.value)
    return other


# -- F01: re-linking the authenticator --------------------------------------------------------


async def test_complete_session_cannot_reenrol_through_login_setup(web):
    secret, _ = await _setup(web)
    before = _user()
    r = await web.get("/login/mfa/setup")
    assert r.status_code == 303 and "crypie_mfa_setup" not in r.headers.get("set-cookie", "")
    r = await web.post("/login/mfa/setup", data={"code": "123456"})
    assert r.status_code == 303
    assert _user().totp_secret == before.totp_secret == secret
    # Without the reauth step /account/mfa is closed too.
    assert (await web.get("/account/mfa")).headers["location"] == "/account"
    assert (await web.post("/account/mfa", data={"code": "1"})).headers["location"] == "/account"


async def test_reenrol_needs_password_and_code_and_keeps_the_expiry(web):
    secret, codes = await _setup(web)
    before = _session_data(web)
    other = _other_browser(web)
    assert (await other.get("/account")).status_code == 200
    r = await web.post("/account", data={"action": "reenrol", "password": "fout", "code": "1"})
    assert r.status_code == 401
    r = await web.post(
        "/account", data={"action": "reenrol", "password": PASSWORD, "code": "000000"}
    )
    assert r.status_code == 401 and "code klopt niet" in r.text
    # A recovery code works as the second factor too (and is used up).
    r = await web.post(
        "/account", data={"action": "reenrol", "password": PASSWORD, "code": codes[0]}
    )
    assert r.status_code == 303 and r.headers["location"] == "/account/mfa"
    reauth = app.state.auth.signer.decode(web.cookies["crypie_reauth"], purpose=REAUTH)
    assert reauth is not None and reauth.expires_at - reauth.issued_at == 300
    r = await web.get("/account/mfa")
    assert r.status_code == 200 and 'action="/account/mfa"' in r.text
    new_secret = _candidate_secret(web)
    r = await web.post("/account/mfa", data={"code": _current_code(new_secret)})
    assert r.status_code == 200 and len(_recovery_codes(r.text)) == 8
    after = _session_data(web)
    assert after is not None and after.expires_at == before.expires_at  # not extended
    assert after.version == before.version + 1
    assert (await web.get("/account")).status_code == 200
    assert (await other.get("/account")).status_code == 303  # every other browser is out
    await other.aclose()
    assert _user().totp_secret == new_secret


async def test_expired_or_foreign_reauth_cookie_is_refused(web):
    await _setup(web)
    user = _user()
    signer = app.state.auth.signer
    old = signer.encode(
        user.username,
        True,
        300,
        now=0,
        purpose=REAUTH,
        account_id=user.account_id,
        version=user.session_version,
    )
    web.cookies.set("crypie_reauth", old)
    assert (await web.get("/account/mfa")).headers["location"] == "/account"
    # A session cookie is not a reauth proof.
    web.cookies.set("crypie_reauth", web.cookies["crypie_session"])
    assert (await web.get("/account/mfa")).headers["location"] == "/account"


# -- F02: revocation ---------------------------------------------------------------------------


def _legacy_token(username: str, mfa: bool = True) -> str:
    """A cookie in the format from before session versions (u/m/i/e, no purpose)."""
    import time

    signer = app.state.auth.signer
    now = int(time.time())
    data = {"u": username, "m": mfa, "i": now, "e": now + 3600}
    payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
    return payload.decode().rstrip("=") + "." + signer._sign(payload).decode()


async def test_old_format_and_mismatched_cookies_are_rejected(web):
    await _setup(web)
    user = _user()
    web.cookies.clear()
    web.cookies.set("crypie_session", _legacy_token("admin"))
    assert (await web.get("/account")).status_code == 303
    signer = app.state.auth.signer
    for kw in (
        {"account_id": "", "version": user.session_version},
        {"account_id": "other", "version": user.session_version},
        {"account_id": user.account_id, "version": user.session_version + 1},
    ):
        web.cookies.set("crypie_session", signer.encode("admin", True, 3600, **kw))
        assert (await web.get("/account")).status_code == 303, kw
    good = {"account_id": user.account_id, "version": user.session_version}
    web.cookies.set("crypie_session", signer.encode("admin", True, 3600, **good))
    assert (await web.get("/account")).status_code == 200


async def test_password_change_and_logout_everywhere_end_other_sessions(web):
    secret, _ = await _setup(web)
    other = _other_browser(web)
    r = await web.post(
        "/account",
        data={
            "action": "password",
            "password": PASSWORD,
            "new_password": "nieuw-wachtwoord",
            "new_password_repeat": "nieuw-wachtwoord",
        },
    )
    assert r.status_code == 200
    assert (await web.get("/account")).status_code == 200
    assert (await other.get("/account")).status_code == 303
    await other.aclose()

    other = _other_browser(web)
    _forget_last_code()
    r = await web.post(
        "/account",
        data={
            "action": "recovery",
            "password": "nieuw-wachtwoord",
            "code": _current_code(secret),
        },
    )
    assert r.status_code == 200
    assert (await web.get("/account")).status_code == 200
    assert (await other.get("/account")).status_code == 303
    await other.aclose()

    other = _other_browser(web)
    r = await web.post("/account", data={"action": "logout_all"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await web.get("/account")).status_code == 303
    assert (await other.get("/account")).status_code == 303
    await other.aclose()


async def test_logout_clears_only_this_browser(web):
    await _setup(web)
    other = _other_browser(web)
    await web.post("/logout")
    assert (await web.get("/account")).status_code == 303
    assert (await other.get("/account")).status_code == 200
    await other.aclose()


async def test_cli_revoke_and_delete_account_end_sessions(web, monkeypatch, capsys):
    await _setup(web)
    other = _other_browser(web)
    assert auth_cli(["revoke-sessions"]) == 0
    assert (await web.get("/account")).status_code == 303
    await _setup_again_after_delete(web, monkeypatch, other)
    await other.aclose()


async def _setup_again_after_delete(web, monkeypatch, other):
    secret = _user().totp_secret
    await web.post("/logout")
    await _login(web)
    _forget_last_code()
    await web.post("/login/mfa", data={"code": _current_code(secret)})
    stolen = web.cookies["crypie_session"]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert auth_cli(["delete-account"]) == 2  # no terminal, no --yes: refused
    assert auth_cli(["delete-account", "--yes"]) == 0
    # Same username again: the old cookie belongs to the deleted account.
    await _setup(web)
    other.cookies.clear()
    other.cookies.set("crypie_session", stolen)
    assert (await other.get("/account")).status_code == 303


async def test_existing_account_gets_an_account_id(web):
    await _setup(web)
    with SessionLocal() as db:
        db.query(AuthUser).update({"account_id": ""})
        db.commit()
    assert (await web.get("/account")).status_code == 303  # no id: no valid session
    with SessionLocal() as db:
        app.state.auth.ensure_account_id(db)
    assert len(_user().account_id) >= 16


# -- F03: setup token --------------------------------------------------------------------------


async def test_setup_token_is_logged_and_wrong_tokens_are_throttled(web, caplog):
    caplog.set_level(logging.WARNING, "crypie.auth")
    app.state.auth._setup_hint_at = None
    await web.get("/setup")
    assert f"/setup?token={_setup_token()}" in caplog.text
    for _ in range(5):
        assert (await web.get("/setup?token=fout")).status_code == 403
    r = await web.get(f"/setup?token={_setup_token()}")
    assert r.status_code == 429 and "Te veel" in r.text


@pytest.mark.parametrize(
    "web", [{"SETUP_TOKEN": "eigen-setup-token-van-de-eigenaar"}], indirect=True
)
async def test_setup_token_from_the_environment(web, caplog):
    assert _setup_token() == "eigen-setup-token-van-de-eigenaar"
    r = await web.get("/setup?token=eigen-setup-token-van-de-eigenaar")
    assert r.status_code == 200
    caplog.set_level(logging.WARNING, "crypie.auth")
    app.state.auth._setup_hint_at = None
    await web.get("/setup")
    assert "eigen-setup-token-van-de-eigenaar" not in caplog.text


async def test_cli_create_account_and_enrol_with_the_printed_code(web, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PASSWORD}\n"))
    assert auth_cli(["create-account", "admin"]) == 0
    out = capsys.readouterr().out
    enrol = re.search(r"\(geldig 60 minuten\): (\S+)", out).group(1)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PASSWORD}\n"))
    assert auth_cli(["create-account", "nog-een"]) == 1
    r = await _login(web)
    assert r.headers["location"] == "/login/mfa/setup?next=/"
    r = await web.get("/login/mfa/setup")
    assert r.status_code == 200 and 'name="enrol_token"' in r.text
    assert "crypie_mfa_setup" not in web.cookies
    r = await web.post("/login/mfa/setup", data={"enrol_token": enrol})
    assert r.status_code == 303
    r = await web.get("/login/mfa/setup")
    assert r.status_code == 200 and "<svg" in r.text
    secret = _candidate_secret(web)
    r = await web.post("/login/mfa/setup", data={"code": _current_code(secret)})
    assert r.status_code == 200 and "Herstelcodes" in r.text
    assert (await web.get("/account")).status_code == 200
    with SessionLocal() as db:  # the code is used up
        assert not app.state.auth.check_enrol_token(db, enrol)


# -- F08: enrolling after reset-mfa needs the CLI code ------------------------------------------


async def test_reset_mfa_needs_the_one_time_code(web, capsys):
    await _setup(web)
    await web.post("/logout")
    assert auth_cli(["reset-mfa"]) == 0
    enrol = re.search(r"\(geldig 60 minuten\): (\S+)", capsys.readouterr().out).group(1)
    await _login(web)
    r = await web.get("/login/mfa/setup")
    assert r.status_code == 200 and "Code van de server" in r.text
    # Without the code no candidate secret is handed out and nothing can be enrolled.
    r = await web.post("/login/mfa/setup", data={"code": "123456"})
    assert r.status_code == 400 and _user().totp_secret is None
    r = await web.post("/login/mfa/setup", data={"enrol_token": "fout"})
    assert r.status_code == 401
    # Expired codes do not work.
    with SessionLocal() as db:
        db.query(AuthUser).update({"enrol_token_expires_at": utcnow() - timedelta(minutes=1)})
        db.commit()
    assert (await web.post("/login/mfa/setup", data={"enrol_token": enrol})).status_code == 401
    with SessionLocal() as db:
        db.query(AuthUser).update({"enrol_token_expires_at": utcnow() + timedelta(minutes=5)})
        db.commit()
    assert (await web.post("/login/mfa/setup", data={"enrol_token": enrol})).status_code == 303
    assert (await web.get("/login/mfa/setup")).status_code == 200
    secret = _candidate_secret(web)
    assert (
        await web.post("/login/mfa/setup", data={"code": _current_code(secret)})
    ).status_code == 200


# -- F04 / F71: throttling ---------------------------------------------------------------------


def _req(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_client_key_groups_ipv6_per_64():
    assert _client_key(_req("2001:db8:1:2:aaaa::1")) == _client_key(_req("2001:db8:1:2:ffff::9"))
    assert _client_key(_req("2001:db8:1:2::1")) == "ip:2001:db8:1:2::/64"
    assert _client_key(_req("2001:db8:1:3::1")) != _client_key(_req("2001:db8:1:2::1"))
    assert _client_key(_req("::ffff:10.0.0.1")) == "ip:10.0.0.1"
    assert _client_key(_req("192.0.2.7")) == "ip:192.0.2.7"
    assert _client_key(_req("weird host\n")) == "ip:weirdhost"


def test_throttle_decays_persists_and_stays_bounded():
    t = LoginThrottle(max_failures=2, base_block_seconds=10, decay_seconds=100, max_entries=5)
    with SessionLocal() as db:
        db.query(AuthThrottle).delete()
        db.commit()
        t.record_failure(db, "k", now=0)
        t.record_failure(db, "k", now=1)
        assert t.blocked_for(db, "k", now=2) > 0
        # A new instance (= a restart) still knows.
        assert LoginThrottle(max_failures=2).blocked_for(db, "k", now=2) > 0
        assert t.failures(db, "k", now=50) == 2
        assert t.failures(db, "k", now=200) == 0  # decayed
        t.record_failure(db, "k", now=200)
        assert t.failures(db, "k", now=200) == 1
        for i in range(20):
            t.record_failure(db, f"many-{i}", now=300 + i)
        assert db.query(AuthThrottle).count() <= 5
        db.query(AuthThrottle).delete()
        db.commit()


async def test_password_stage_is_never_locked_per_account(web):
    await _setup(web)
    await web.post("/logout")
    attacker = AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 1)), base_url="http://test"
    )
    for _ in range(8):
        await attacker.post("/login", data={"username": "admin", "password": "fout"})
    assert (
        await attacker.post("/login", data={"username": "admin", "password": PASSWORD})
    ).status_code == 429
    await attacker.aclose()
    assert (await _login(web)).status_code == 303  # the owner, from another address


def _browser(ip: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(ip, 1)), base_url="http://test")


async def _spread_wrong_passwords(count: int, prefix: str = "203.0.113") -> None:
    """``count`` wrong passwords, at most 4 per address: never the per-address wait."""
    for i in range(count):
        if i % 4 == 0:
            c = _browser(f"{prefix}.{i // 4 + 1}")
        r = await c.post("/login", data={"username": "admin", "password": "fout"})
        assert r.status_code in (401, 429)
        if i % 4 == 3 or i == count - 1:
            await c.aclose()


async def test_password_guesses_spread_over_addresses_share_an_account_wide_wait(web):
    await _setup(web)  # this browser completed a login: it is known
    await web.post("/logout")
    assert "crypie_device" in web.cookies  # logging out keeps it
    await _spread_wrong_passwords(routes_service.PASSWORD_ACCOUNT_MAX_FAILURES)
    # A browser that never logged in, from yet another address: held up, even with the password.
    stranger = _browser("198.51.100.200")
    r = await _login(stranger)
    assert r.status_code == 429 and "clear-throttle" in r.text and "eerder" in r.text
    await stranger.aclose()
    # The owner's known browser is never held up by the account-wide limit.
    assert (await _login(web)).status_code == 303


async def test_wrong_passwords_from_a_known_browser_do_not_count_account_wide(web):
    await _setup(web)
    await web.post("/logout")
    for _ in range(4):
        assert (await _login(web, "fout")).status_code == 401
    with SessionLocal() as db:
        keys = [row.key for row in db.query(AuthThrottle).all()]
    assert keys and not any(k.startswith("pw:") for k in keys)


async def test_known_browser_cookie_is_set_after_a_login_and_bound_to_the_account(web):
    secret, _ = await _setup(web)
    auth = app.state.auth
    user = _user()
    token = web.cookies["crypie_device"]
    assert auth.known_device(user, token)
    # Set again by a normal password-plus-code login in a fresh browser.
    fresh = _browser("192.0.2.50")
    await _login(fresh)
    _forget_last_code()
    assert (await fresh.post("/login/mfa", data={"code": _current_code(secret)})).status_code == 303
    assert auth.known_device(user, fresh.cookies["crypie_device"])
    await fresh.aclose()
    # A password alone does not make a browser known.
    half = _browser("192.0.2.51")
    await _login(half)
    assert "crypie_device" not in half.cookies
    await half.aclose()
    # Not for another account, another purpose or a forged signature.
    other = SimpleNamespace(account_id="someone-else", username=user.username)
    assert not auth.known_device(other, token)
    assert not auth.known_device(user, auth.full_token(user))
    assert not auth.known_device(user, token[:-2] + ("00" if token[-2:] != "00" else "11"))
    assert not auth.known_device(user, None)


def test_account_wide_password_rows_are_never_evicted():
    t = LoginThrottle(max_failures=2, decay_seconds=10_000, max_entries=5)
    with SessionLocal() as db:
        db.query(AuthThrottle).delete()
        db.commit()
        t.record_failure(db, "pw:acct", now=0)
        t.record_failure(db, "mfa:acct", now=0)
        for i in range(20):
            t.record_failure(db, f"ip:10.0.0.{i}", now=10 + i)
        assert t.failures(db, "pw:acct", now=100) == 1
        assert t.failures(db, "mfa:acct", now=100) == 1
        db.query(AuthThrottle).delete()
        db.commit()


async def test_second_factor_is_throttled_per_account(web):
    secret, _ = await _setup(web)
    await web.post("/logout")
    for i in range(5):
        c = AsyncClient(
            transport=ASGITransport(app=app, client=(f"198.51.100.{i}", 1)), base_url="http://test"
        )
        await _login(c)
        assert (await c.post("/login/mfa", data={"code": "000000"})).status_code == 401
        await c.aclose()
    c = AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.77", 1)), base_url="http://test"
    )
    await _login(c)
    _forget_last_code()
    r = await c.post("/login/mfa", data={"code": _current_code(secret)})
    assert r.status_code == 429  # the account is blocked for a while, whatever the address
    await c.aclose()
    assert (await _login(web)).status_code == 303  # the password stage itself stays open


# -- F05: PBKDF2 off the event loop --------------------------------------------------------


async def test_login_answers_429_when_all_hash_slots_are_busy(web):
    await _setup(web)
    await web.post("/logout")
    taken = 0
    while routes._HASH_SLOTS.acquire(blocking=False):
        taken += 1
    try:
        r = await _login(web)
        assert r.status_code == 429 and "druk" in r.text
    finally:
        for _ in range(taken):
            routes._HASH_SLOTS.release()
    assert (await _login(web)).status_code == 303


# -- F06: cross-origin protection ----------------------------------------------------------


@pytest.mark.parametrize(
    "headers, allowed",
    [
        ({}, True),
        ({"sec-fetch-site": "same-origin"}, True),
        ({"sec-fetch-site": "none"}, True),
        ({"sec-fetch-site": "same-site"}, False),
        ({"sec-fetch-site": "cross-site"}, False),
        ({"sec-fetch-site": "same-site", "origin": "http://test"}, False),
        ({"origin": "http://test"}, True),
        ({"origin": "http://evil.5-6-7-8.sslip.io"}, False),
        ({"origin": "https://test"}, False),
        ({"origin": "null"}, False),
        ({"origin": "https://crypie.example.nl"}, True),
    ],
)
@pytest.mark.parametrize("web", [{"TRUSTED_ORIGINS": "https://crypie.example.nl/"}], indirect=True)
async def test_cross_origin_posts_are_refused(web, headers, allowed):
    r = await web.post("/login", data={"username": "x", "password": "y"}, headers=headers)
    if allowed:
        assert r.status_code != 403
    else:
        assert r.status_code == 403 and "andere website" in r.text
    # Safe methods always pass.
    assert (await web.get("/login", headers=headers)).status_code == 200


async def test_cross_site_requests_cannot_reach_orders_or_settings(web):
    await _setup(web)
    evil = {"sec-fetch-site": "same-site", "origin": "https://evil.sslip.io"}
    for path, data in (
        ("/orders", {"market": "BTC-EUR", "side": "buy", "amount_quote": "10"}),
        ("/settings", {"max_trade_eur": "1000000"}),
        ("/logout", {}),
        ("/account", {"action": "logout_all"}),
    ):
        assert (await web.post(path, data=data, headers=evil)).status_code == 403, path
    assert (await web.get("/account")).status_code == 200  # still logged in


# -- F22 / F26 / F14: headers -----------------------------------------------------------------


async def test_security_headers_on_every_response(web):
    for path in ("/login", "/health", "/static/app.css", "/nope"):
        r = await web.get(path)
        assert r.headers["x-frame-options"] == "DENY", path
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "script-src 'self';" in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert "strict-transport-security" not in r.headers  # plain http
    assert (await web.get("/login")).headers["cache-control"] == "no-store"
    assert (await web.post("/login", data={})).headers["cache-control"] == "no-store"
    assert (await web.get("/setup")).headers["cache-control"] == "no-store"
    secret, _ = await _setup(web)
    assert (await web.get("/account")).headers["cache-control"] == "no-store"


async def test_auth_pages_have_no_inline_scripts(web):
    pages = []
    pages.append((await web.get("/login")).text)
    pages.append((await web.get("/setup")).text)
    pages.append((await web.get(f"/setup?token={_setup_token()}")).text)
    r = await web.post(
        "/setup",
        data={
            "username": "admin",
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "token": _setup_token(),
        },
    )
    pages.append((await web.get("/login/mfa/setup")).text)
    secret = _candidate_secret(web)
    r = await web.post("/login/mfa/setup", data={"code": _current_code(secret)})
    pages.append(r.text)
    assert 'data-copy="' in r.text and "onclick" not in r.text  # F27
    await web.post("/logout")
    await _login(web)
    pages.append((await web.get("/login/mfa")).text)
    for html in pages:
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), html[:200]
        assert not re.search(r"\son[a-z]+\s*=", html), html[:200]
    assert (await web.get("/static/auth.js")).status_code == 200


# -- F07: __Host- cookies over https ----------------------------------------------------------


@pytest.mark.parametrize("web", [{"base_url": "https://test"}], indirect=True)
async def test_https_uses_host_prefixed_cookies_and_hsts(web):
    r = await web.get("/login")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    r = await web.post(
        "/setup",
        data={
            "username": "admin",
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "token": _setup_token(),
        },
    )
    set_cookie = r.headers["set-cookie"]
    assert set_cookie.startswith("__Host-crypie_session=") and "Secure" in set_cookie
    assert "Path=/" in set_cookie and "Domain" not in set_cookie
    r = await web.get("/login/mfa/setup")
    secret = app.state.auth.signer.decode(
        web.cookies["__Host-crypie_mfa_setup"], purpose=MFA_SETUP
    ).extra["secret"]
    r = await web.post("/login/mfa/setup", data={"code": _current_code(secret)})
    assert r.status_code == 200
    assert (await web.get("/account")).status_code == 200
    # A plain-named cookie (e.g. tossed in by a sibling domain) is not read over https.
    token = web.cookies["__Host-crypie_session"]
    web.cookies.clear()
    web.cookies.set("crypie_session", token)
    assert (await web.get("/account")).status_code == 303


# -- F09 / F10: token purposes and lifetimes --------------------------------------------------


def test_tokens_are_bound_to_their_purpose_and_lifetime():
    signer = SessionSigner("s3cret", {SESSION: 3600})
    session = signer.encode("admin", True, 3600, now=1000)
    assert signer.decode(session, now=1001) is not None
    assert signer.decode(session, now=1001, purpose=MFA_SETUP) is None
    assert signer.decode(session, now=1001, purpose=REAUTH) is None
    setup = signer.encode("admin", False, 600, now=1000, purpose=MFA_SETUP, extra={"secret": "x"})
    assert (
        signer.decode(setup, now=1001) is None
        and signer.decode(setup, now=1001, purpose=LOGIN) is None
    )
    assert signer.decode(setup, now=1001, purpose=MFA_SETUP).extra == {"secret": "x"}
    # Longer than the purpose allows (a forged 31-year cookie) or issued in the future.
    assert signer.decode(signer.encode("admin", True, 3601, now=1000), now=1001) is None
    assert signer.decode(signer.encode("admin", True, 999 * 86400, now=1000), now=1001) is None
    assert signer.decode(signer.encode("admin", True, 60, now=5000), now=1000) is None
    assert signer.decode(signer.encode("admin", True, 60, now=1030), now=1000) is not None  # skew
    assert signer.decode(signer.encode("admin", True, 60, now=1000, purpose="other"), now=1) is None


async def test_session_cookie_is_not_accepted_as_setup_secret(web):
    await _setup(web)
    await web.post("/logout")
    with SessionLocal() as db:
        app.state.auth.reset_totp(db)
        enrol = app.state.auth.issue_enrol_token(db)
    await _login(web)
    await web.post("/login/mfa/setup", data={"enrol_token": enrol})
    web.cookies.set("crypie_mfa_setup", web.cookies["crypie_session"])
    r = await web.post("/login/mfa/setup", data={"code": "123456"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login/mfa/setup")
    assert _user().totp_secret is None


async def test_recovery_codes_are_hashed_with_a_key_and_old_hashes_still_work(web):
    _, codes = await _setup(web)
    stored = json.loads(_user().recovery_codes)
    assert all(h.startswith("h1$") for h in stored)
    assert _legacy_hash_code(codes[0]) not in stored
    with SessionLocal() as db:  # a code stored before keyed hashes existed
        db.query(AuthUser).update(
            {"recovery_codes": json.dumps([_legacy_hash_code("abcde-fghjk")])}
        )
        db.commit()
    await web.post("/logout")
    await _login(web)
    assert (await web.post("/login/mfa", data={"code": "abcde-fghjk"})).status_code == 303
    assert json.loads(_user().recovery_codes) == []


@pytest.mark.parametrize("web", [{"SESSION_SECRET": "kort"}], indirect=True)
async def test_weak_session_secret_is_flagged(web):
    assert app.state.session_secret_weak is True


@pytest.mark.parametrize("web", [{"SESSION_SECRET": "x" * 40}], indirect=True)
async def test_strong_session_secret_is_not_flagged(web):
    assert app.state.session_secret_weak is False


# -- F11 / F12 / F13 ------------------------------------------------------------------------------


def test_safe_next_refuses_backslashes_and_control_characters():
    assert _safe_next("/\\evil.example") == "/"
    assert _safe_next("/news\\x") == "/"
    assert _safe_next("/news\t") == "/"
    assert _safe_next("/\x7fx") == "/"
    assert _safe_next("/news?x=1") == "/news?x=1"


def test_totp_accepts_ascii_digits_only():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    arabic = "".join(chr(0x0660 + int(d)) for d in "287082")
    assert totp.verify(secret, "287082", now=59) == 1
    assert totp.verify(secret, arabic, now=59) is None
    assert SessionSigner("s").decode("é.abc") is None
    assert SessionSigner("s").decode("abc.é") is None


async def test_unicode_input_gives_no_server_error(web):
    await _setup(web)
    await web.post("/logout")
    await _login(web)
    r = await web.post("/login/mfa", data={"code": "١٢٣٤٥٦"})
    assert r.status_code == 401
    r = await web.get("/account", headers={"cookie": "crypie_session=été.abc".encode()})
    assert r.status_code == 303


def test_password_verification_has_no_plaintext_fallback_and_a_floor():
    assert not verify_password("geheim", "geheim")
    weak = hash_password("geheim", iterations=1000)
    assert not verify_password("geheim", weak)
    assert not verify_password("geheim", "pbkdf2_sha256$999999999999$AAAA$AAAA")


async def test_login_upgrades_an_old_hash(web):
    await _setup(web)
    with SessionLocal() as db:
        db.query(AuthUser).update(
            {"password_hash": hash_password(PASSWORD, iterations=MIN_ITERATIONS)}
        )
        db.commit()
    await web.post("/logout")
    assert (await _login(web)).status_code == 303
    assert _user().password_hash.split("$")[1] == str(ITERATIONS)


# -- F15 / F16 / F17 / F25 / F52 / F62 -----------------------------------------------------------


async def test_cli_never_takes_the_password_as_argument(web):
    with pytest.raises(SystemExit):
        auth_cli(["reset-password", "geheim-wachtwoord"])


def test_live_trading_needs_the_login():
    client = BitvavoClient("key", "secret", "https://api.bitvavo.com/v2")
    on = Settings(auth_enabled=True, live_trading_ack=True)
    off = Settings(auth_enabled=False, live_trading_ack=True)
    assert live_blocker(on, client) is None
    assert "AUTH_ENABLED" in live_blocker(off, client)


async def test_api_docs_are_off_by_default(web):
    await _setup(web)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await web.get(path)).status_code == 404, path
    assert 'href="/docs"' not in (await web.get("/settings")).text


async def test_failed_login_does_not_log_the_username(web, caplog):
    await _setup(web)
    await web.post("/logout")
    caplog.set_level(logging.WARNING)
    await web.post("/login", data={"username": "mijn-wachtwoord-per-ongeluk", "password": "x"})
    assert "failed login" in caplog.text
    assert "mijn-wachtwoord-per-ongeluk" not in caplog.text


async def test_request_bodies_are_limited(web):
    big = "x" * (PUBLIC_MAX_BODY_BYTES + 1)
    r = await web.post("/login", data={"username": "admin", "password": big})
    assert r.status_code == 413
    r = await web.post("/setup", content=b"a=" + b"x" * PUBLIC_MAX_BODY_BYTES)
    assert r.status_code == 413
    r = await web.post("/login", content=b"x", headers={"content-length": "abc"})
    assert r.status_code == 400

    async def chunks():  # streamed without Content-Length
        for _ in range(4):
            yield b"x" * (PUBLIC_MAX_BODY_BYTES // 2)

    r = await web.post(
        "/login", content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert r.status_code == 413
    await _setup(web)
    r = await web.put(
        "/api/settings",
        content=b"{" + b" " * MAX_BODY_BYTES + b"}",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
    # Normal sizes still work everywhere.
    assert (await web.put("/api/settings", json={})).status_code != 413


async def test_setup_looks_the_same_with_an_account(web):
    without = await web.get("/setup")
    await _setup(web)
    await web.post("/logout")
    with_account = await web.get("/setup")
    assert without.status_code == with_account.status_code == 404
    assert without.text == with_account.text


def test_old_auth_users_table_is_migrated(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db import add_missing_columns

    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE auth_users (username VARCHAR(64) PRIMARY KEY, "
                "password_hash VARCHAR(256), created_at DATETIME, totp_secret VARCHAR(64), "
                "totp_enrolled_at DATETIME, totp_last_counter INTEGER, recovery_codes TEXT, "
                "last_login_at DATETIME)"
            )
        )
        conn.execute(text("INSERT INTO auth_users (username, password_hash) VALUES ('admin', 'x')"))
    add_missing_columns(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("auth_users")}
    assert {"account_id", "session_version", "enrol_token_hash"} <= columns
    with engine.begin() as conn:
        row = conn.execute(text("SELECT account_id, session_version FROM auth_users")).one()
    assert tuple(row) == ("", 0)
    engine.dispose()
