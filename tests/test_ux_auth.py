"""The login flow and the account page after the UX audit: recovery-code mode, lockout and
session messages, enrolment that can be restarted, recovery-code tools, step numbering and the
account page (UX-50, UX-61 .. UX-67)."""

import html
import io
import re

from app.auth import routes
from app.auth.__main__ import main as auth_cli
from app.auth.session import COOKIE_NAME
from app.db import AuthThrottle, AuthUser, SessionLocal
from app.flash import flashes_from_response
from app.main import app
from tests.test_auth import (
    PASSWORD,
    _candidate_secret,
    _current_code,
    _forget_last_code,
    _login,
    _recovery_codes,
    _setup,
    _setup_token,
    client,  # noqa: F401  (fixture)
)


def _user() -> AuthUser:
    with SessionLocal() as db:
        return db.query(AuthUser).one()


def _clear_throttle() -> None:
    with SessionLocal() as db:
        db.query(AuthThrottle).delete()
        db.commit()


def _text(r) -> str:
    return html.unescape(r.text)


# -- UX-50: recovery codes can be typed on a phone ------------------------------------------------


async def test_recovery_code_has_its_own_field_with_a_normal_keyboard(client):  # noqa: F811
    await _setup(client)
    await client.post("/logout")
    await _login(client)
    page = (await client.get("/login/mfa")).text
    fields = re.findall(r'<input class="code-input[^"]*" type="text" name="code"[^>]*>', page)
    assert len(fields) == 2
    app_field, recovery_field = fields
    assert 'inputmode="numeric"' in app_field and "autofocus" in app_field
    assert 'inputmode="numeric"' not in recovery_field and 'inputmode="text"' in recovery_field
    assert 'autocapitalize="off"' in recovery_field and "[A-Za-z0-9 \\-]" in recovery_field
    assert "Herstelcode gebruiken" in page
    # A wrong recovery code opens that part again, with the focus in its field.
    r = await client.post("/login/mfa", data={"code": "abcde-fghjk"})
    assert r.status_code == 401 and "herstelcode klopt niet" in r.text
    assert '<details class="auth-details" id="herstelcode" open>' in r.text
    recovery_field = re.findall(r'<input class="code-input[^"]*"[^>]*>', r.text)[1]
    assert "autofocus" in recovery_field and 'aria-invalid="true"' in recovery_field


async def test_login_with_a_recovery_code_says_how_many_are_left(client):  # noqa: F811
    _, codes = await _setup(client)
    await client.post("/logout")
    await _login(client)
    r = await client.post("/login/mfa", data={"code": codes[0].replace("-", " ")})
    assert r.status_code == 303 and r.headers["location"] == "/"
    message = flashes_from_response(r)[-1]  # 'Je bent uitgelogd.' may still be pending
    assert "ingelogd met een herstelcode" in message["text"]
    assert "nog 7 van 8" in message["text"] and "Account" in message["text"]
    assert message["kind"] == "info"
    page = (await client.get("/")).text
    assert "nog 7 van 8" in page


# -- UX-62: lockout in plain words, and what you can do ------------------------------------------


async def test_wrong_passwords_warn_before_the_wait_and_give_it_in_minutes(client):  # noqa: F811
    await _setup(client)
    await client.post("/logout")
    _clear_throttle()
    texts = []
    for _ in range(app.state.auth.throttle.max_failures):
        r = await _login(client, "fout-wachtwoord")
        texts.append(_text(r))
    assert "Nog" not in texts[0] and "Nog 2 pogingen" in texts[2] and "Nog 1 poging;" in texts[3]
    assert re.search(r"Probeer het over \d+ seconden opnieuw \(om \d\d:\d\d UTC\)", texts[4])
    r = await _login(client)
    assert r.status_code == 429 and "clear-throttle" in r.text
    assert routes._wait_message(900).startswith(
        "Te veel mislukte pogingen. Probeer het over 15 minuten opnieuw (om "
    )
    assert "1 minuut" in routes._wait_message(45 + 15)
    _clear_throttle()


async def test_login_page_tells_what_to_do_when_locked_out_or_lost(client):  # noqa: F811
    page = (await client.get("/login")).text
    assert "Wachtwoord of authenticator kwijt?" in page
    for command in ("reset-password", "reset-mfa", "clear-throttle"):
        assert command in page
    # After an error the name stays and the focus goes to the password.
    await _setup(client)
    await client.post("/logout")
    r = await _login(client, "fout-wachtwoord")
    username = re.search(r'<input type="text" name="username"[^>]*>', r.text).group(0)
    password = re.search(r'<input type="password" name="password"[^>]*>', r.text).group(0)
    assert "autofocus" not in username and "autofocus" in password
    assert 'aria-describedby="login-error"' in password and 'id="login-error"' in r.text
    _clear_throttle()


# -- UX-63: why you are at /login, and back to where you were -------------------------------------


async def test_expired_session_is_explained_and_the_page_is_kept(client):  # noqa: F811
    await _setup(client)
    with SessionLocal() as db:  # 'Overal uitloggen' from another browser
        app.state.auth.revoke_sessions(db)
    r = await client.get("/journal?market=BTC-EUR&days=7")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/journal?market=BTC-EUR%26days=7"
    (message,) = flashes_from_response(r)
    assert "sessie is verlopen" in message["text"]
    r = await client.post("/settings", data={"group": "limits", "max_trade_eur": "40"})
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/settings"
    assert "niet opgeslagen" in flashes_from_response(r)[-1]["text"]
    page = (await client.get("/login?next=/settings")).text
    assert "je wijziging is niet opgeslagen" in page and 'value="/settings"' in page
    # Without any earlier login there is nothing to explain.
    client.cookies.clear()
    r = await client.get("/trading")
    assert flashes_from_response(r) == []


async def test_logging_out_says_so(client):  # noqa: F811
    await _setup(client)
    r = await client.post("/logout")
    assert r.headers["location"] == "/login"
    assert flashes_from_response(r) == [{"kind": "info", "text": "Je bent uitgelogd."}]
    assert "Je bent uitgelogd." in (await client.get("/login")).text
    await _login(client)
    _forget_last_code()


async def test_password_step_that_expired_is_explained(client):  # noqa: F811
    await _setup(client)
    await client.post("/logout")
    await _login(client)
    signer = app.state.auth.signer
    user = _user()
    old = signer.encode(
        user.username,
        False,
        600,
        now=0,
        purpose="login",
        account_id=user.account_id,
        version=user.session_version,
    )
    client.cookies.set(COOKIE_NAME, old)
    r = await client.get("/login/mfa")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
    assert "duurde te lang" in flashes_from_response(r)[-1]["text"]


# -- UX-61: the first authenticator --------------------------------------------------------------


async def test_enrolment_page_names_the_time_limit_and_what_later_means(client):  # noqa: F811
    r = await client.post(
        "/setup",
        data={
            "username": "admin",
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "token": _setup_token(),
        },
    )
    assert r.status_code == 303
    page = _text(await client.get("/login/mfa/setup"))
    assert "Stap 2 van 3" in page and "Rond dit af binnen 10 minuten" in page
    assert "UTC" in page and "setup-link" in page and "reset-mfa" in page
    later = re.search(r'<form method="post" action="/logout"[^>]*>', page).group(0)
    assert "data-confirm=" in later and "Later koppelen" in page and "Annuleren" not in page
    # UX-67: one numbered list, a described QR code, no stray '3.' in a label.
    steps = re.search(r'<ol class="auth-steps">(.*?)</ol>', page, re.S).group(1)
    assert steps.count("<li>") == 3 and "3. " not in page
    assert re.search(r'<div class="qr" role="img" aria-label="QR-code voor je authenticator', page)


async def test_unfinished_first_enrolment_restarts_with_the_setup_link(client):  # noqa: F811
    token = _setup_token()
    form = {"username": "admin", "password": PASSWORD, "password_repeat": PASSWORD}
    await client.post("/setup", data=form | {"token": token})
    await client.post("/logout")  # 'Later koppelen'
    r = await _login(client)
    assert r.headers["location"] == "/login/mfa/setup?next=/"
    assert 'name="enrol_token"' in (await client.get("/login/mfa/setup")).text
    await client.post("/logout")
    # The setup link plus the password starts the linking again.
    page = (await client.get(f"/setup?token={token}")).text
    assert "Je account bestaat al" in page and 'value="resume"' in page
    resume = {"username": "admin", "token": token, "action": "resume"}
    r = await client.post("/setup", data=resume | {"password": "fout-wachtwoord"})
    assert r.status_code == 401
    with SessionLocal() as db:
        assert app.state.auth.throttle.failures(db, "ip:127.0.0.1") >= 1
    r = await client.post("/setup", data=resume | {"password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa/setup"
    r = await client.get("/login/mfa/setup")
    assert "<svg" in r.text
    secret = _candidate_secret(client)
    r = await client.post("/login/mfa/setup", data={"code": _current_code(secret)})
    assert r.status_code == 200 and len(_recovery_codes(r.text)) == 8
    assert "Stap 3 van 3" in r.text
    # Linked once: the setup link is closed for good.
    await client.post("/logout")
    r = await client.get(f"/setup?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.post("/setup", data=resume | {"password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    _clear_throttle()


async def test_after_reset_mfa_only_the_server_code_works(client, capsys):  # noqa: F811
    token = _setup_token()
    form = {"username": "admin", "password": PASSWORD, "password_repeat": PASSWORD}
    await client.post("/setup", data=form | {"token": token})
    await client.post("/logout")
    assert auth_cli(["reset-mfa"]) == 0  # an enrol code now exists
    capsys.readouterr()
    r = await client.get(f"/setup?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.post(
        "/setup",
        data={"username": "admin", "password": PASSWORD, "token": token, "action": "resume"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert routes.enrolment_unfinished(_user()) is False


async def test_setup_link_restart_closes_an_hour_after_the_account_was_made(client):  # noqa: F811
    """Security review: read access to the logs plus the password must not be enough to link
    an authenticator long after the owner made the account; then only the CLI code works."""
    from datetime import UTC, datetime, timedelta

    token = _setup_token()
    form = {"username": "admin", "password": PASSWORD, "password_repeat": PASSWORD}
    await client.post("/setup", data=form | {"token": token})
    await client.post("/logout")
    user = _user()
    made = user.created_at.replace(tzinfo=UTC)
    assert routes.enrolment_unfinished(user, made + timedelta(minutes=59)) is True
    assert routes.enrolment_unfinished(user, made + timedelta(minutes=61)) is False
    with SessionLocal() as db:
        db.query(AuthUser).update({"created_at": datetime.now(UTC) - timedelta(hours=2)})
        db.commit()
    r = await client.get(f"/setup?token={token}")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    resume = {"username": "admin", "token": token, "action": "resume", "password": PASSWORD}
    r = await client.post("/setup", data=resume)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


async def test_setup_link_restart_only_works_in_the_browser_that_made_the_account(client):  # noqa: F811
    """Security review round 3: the setup token from the log plus the password, in another
    browser, must not link an authenticator, not even in the first hour."""
    from httpx import ASGITransport, AsyncClient

    token = _setup_token()
    form = {"username": "admin", "password": PASSWORD, "password_repeat": PASSWORD}
    r = await client.post("/setup", data=form | {"token": token})
    assert routes.SETUP_RESUME_COOKIE in r.headers.get("set-cookie", "")
    await client.post("/logout")  # 'Later koppelen' keeps the resume cookie
    assert routes.enrolment_unfinished(_user()) is True
    resume = {"username": "admin", "token": token, "action": "resume", "password": PASSWORD}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        r = await other.get(f"/setup?token={token}")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "browser waarin je het account aanmaakte" in flashes_from_response(r)[0]["text"]
        r = await other.post("/setup", data=resume)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert COOKIE_NAME + "=" not in r.headers.get("set-cookie", "")
        # A resume cookie of another account (or a forged one) does not open it either.
        other.cookies.set(routes.SETUP_RESUME_COOKIE, "eyJwIjoic2V0dXBfcmVzdW1lIn0.fout")
        r = await other.post("/setup", data=resume)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    # The browser that made the account still can.
    r = await client.post("/setup", data=resume)
    assert r.status_code == 303 and r.headers["location"] == "/login/mfa/setup"
    _clear_throttle()


def test_startup_log_never_shows_the_setup_token_once_the_account_exists(caplog):
    from app.auth.service import AuthService
    from app.config import get_settings
    from app.main import build_auth

    with SessionLocal() as db:
        db.query(AuthUser).delete()
        db.add(AuthUser(username="admin", password_hash="x"))
        db.commit()
    settings = get_settings().model_copy(
        update={"setup_token": "", "auth_enabled": True, "session_secret": "s" * 40}
    )
    with caplog.at_level("WARNING"):
        auth = build_auth(settings)
    assert isinstance(auth, AuthService)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "reset-mfa" in logged and auth.setup_token not in logged
    with SessionLocal() as db:
        db.query(AuthUser).delete()
        db.commit()


async def test_enrolment_redirects_keep_the_whole_next_address(client):  # noqa: F811
    token = _setup_token()
    form = {"username": "admin", "password": PASSWORD, "password_repeat": PASSWORD}
    await client.post("/setup", data=form | {"token": token})
    await client.post("/logout")
    await client.post(
        "/login",
        data={"username": "admin", "password": PASSWORD, "next": "/news?hours=6&telt=nee"},
    )
    target = "/news?hours=6%26telt=nee"
    r = await client.get("/login/mfa", params={"next": "/news?hours=6&telt=nee"})
    assert r.status_code == 303 and r.headers["location"] == f"/login/mfa/setup?next={target}"
    r = await client.post(
        "/login/mfa/setup", data={"enrol_token": "fout", "next": "/news?hours=6&telt=nee"}
    )
    assert r.status_code == 401 and 'value="/news?hours=6&amp;telt=nee"' in r.text
    _clear_throttle()


async def test_setup_link_needs_the_right_token_for_a_restart(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{PASSWORD}\n"))
    r = await client.get("/setup?token=fout")
    assert r.status_code == 403
    r = await client.post(
        "/setup",
        data={"username": "admin", "password": PASSWORD, "token": "fout", "action": "resume"},
    )
    assert r.status_code == 403
    _clear_throttle()


# -- UX-64: recovery codes ------------------------------------------------------------------------


async def test_recovery_codes_page_offers_download_print_and_a_check(client):  # noqa: F811
    r = await client.post(
        "/setup",
        data={
            "username": "admin",
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "token": _setup_token(),
        },
    )
    await client.get("/login/mfa/setup")
    secret = _candidate_secret(client)
    r = await client.post("/login/mfa/setup", data={"code": _current_code(secret)})
    page = r.text
    assert "Stap 3 van 3 · herstelcodes bewaren" in page
    assert 'data-download="' in page and 'data-filename="crypie-herstelcodes.txt"' in page
    assert "data-print" in page and 'data-copy-target="recovery-codes"' in page
    assert '<p class="recovery-status" role="status" data-copy-status></p>' in page
    assert "Ik heb de codes bewaard" in page and "data-continue" in page
    assert "na vernieuwen of" in _text(r)
    assert "/static/auth.js" in page and "/static/theme.js" in page


# -- UX-65 / UX-67: account page and steps --------------------------------------------------------


async def test_account_page_groups_status_changes_and_destructive_actions(client):  # noqa: F811
    await _setup(client)
    page = (await client.get("/account")).text
    assert "Deze sessie geldig tot" in page and "24 uur" in page
    for anchor in ('id="wachtwoord"', 'id="herstelcodes"', 'id="authenticator"'):
        assert anchor in page
    logout_all = re.search(r'<form method="post" action="/account" data-confirm="[^"]*">', page)
    assert logout_all and "Overal uitloggen?" in logout_all.group(0)
    assert re.search(r'id="herstelcodes".*?data-confirm="Nieuwe herstelcodes maken', page, re.S)
    assert page.count('class="btn btn-danger"') == 3
    assert "reset-password" in page and "clear-throttle" in page
    # An error appears in the card of the form that was sent, with its field marked.
    r = await client.post(
        "/account", data={"action": "recovery", "password": PASSWORD, "code": "000000"}
    )
    assert r.status_code == 401
    card = r.text.split('id="herstelcodes"', 1)[1].split("</details>", 1)[0]
    assert "code klopt niet" in card and 'id="recovery-error"' in card
    assert re.search(r'name="code"[^>]*aria-invalid="true"', card)
    assert (
        '<details class="account-action account-action--danger" id="herstelcodes" open>' in r.text
    )
    _forget_last_code()
    _clear_throttle()


async def test_logout_everywhere_says_so_on_the_login_page(client):  # noqa: F811
    await _setup(client)
    r = await client.post("/account", data={"action": "logout_all"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "overal uitgelogd" in flashes_from_response(r)[0]["text"]


async def test_steps_are_numbered_out_of_three_for_a_new_account(client):  # noqa: F811
    page = (await client.get(f"/setup?token={_setup_token()}")).text
    assert "Stap 1 van 3 · account aanmaken" in page
    login = (await client.get("/login")).text
    assert "Stap 1 van 2 · wachtwoord" in login
    assert "<title>Inloggen · Crypie</title>" in login
    assert "/static/icons/favicon.svg" in login and "data:image/svg" not in login


async def test_setup_token_page_shows_no_messages(client):  # noqa: F811
    await _setup(client)
    await client.post("/logout")  # leaves 'Je bent uitgelogd.' pending
    page = (await client.get("/setup")).text
    assert "uitgelogd" not in page
