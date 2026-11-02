"""The contracts between the front-end and back-end foundation: the bot status from
bot_health() through ui.status, the kill switch's return address, flashes on every layout and
error pages inside the page frame."""

from datetime import timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.db import SessionLocal
from app.errors import LOGIN_OFF_MESSAGE
from app.flash import flashes_from_response
from app.main import app
from app.trading import risk
from app.trading.health import PAUSED_DETAIL
from app.web import templates
from tests.test_health import (
    NOW,
    engine_,
    health,
    session,  # noqa: F401 - in-memory database fixture
)
from tests.test_security_auth import web  # noqa: F401 - the login-on fixture

HTML = {"accept": "text/html,application/xhtml+xml"}
BASE = "https://api.bitvavo.com/v2"


@pytest.fixture
async def api():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _status(h) -> str:
    page = templates.env.from_string('{% import "_macros.html" as ui %}{{ ui.status(h) }}')
    return page.render(h=h)


def test_status_macro_renders_what_bot_health_returns(session):  # noqa: F811
    eng = engine_(session)
    html = _status(health(eng, session))
    assert "status-ok" in html and "pill-green" in html and "Actief" in html
    assert "Laatste cyclus:" in html and "Volgende:" in html and "laatst gelukt" in html

    # Blocked: the one sentence is shown once, not again in the list of reasons.
    blocked = engine_(session, available=False)
    h = health(blocked, session)
    assert h["level"] == "warn" and h["detail"] in h["reasons"]
    html = _status(h)
    assert html.count(h["detail"]) == 1 and "pill-orange" in html

    # Kill switch on and the exit check failing: an error, with the pause as an extra reason.
    risk.set_paused(session, True)
    eng.exit_failing_since = NOW - timedelta(minutes=7)
    eng.exit_error = "koersen niet op te halen (time-out)"
    h = health(eng, session)
    html = _status(h)
    assert "status-error" in html and "pill-red" in html and "loopt achter" in html
    assert html.count(h["detail"]) == 1 and "Noodstop actief: kopen gepauzeerd." in html

    eng.exit_failing_since = eng.exit_error = None
    html = _status(health(eng, session))
    assert "status-paused" in html and PAUSED_DETAIL in html


async def test_kill_switch_returns_to_the_page_it_was_used_on(api):
    try:
        r = await api.post("/trading/action", data={"action": "pause", "next": "/"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        with SessionLocal() as s:
            assert risk.is_paused(s)
        r = await api.post("/trading/action", data={"action": "resume", "next": "/trading"})
        assert r.headers["location"] == "/trading"
        with SessionLocal() as s:
            assert not risk.is_paused(s)
        # Only paths on this site; anything else goes to the overview.
        for target in ("//evil.example/x", "https://evil.example/", "/\\evil.example", "/login"):
            r = await api.post("/trading/action", data={"action": "resume", "next": target})
            assert r.status_code == 303 and r.headers["location"] == "/", target
        # Without next (the buttons on Handelen): back to Handelen, as before.
        r = await api.post("/trading/action", data={"action": "resume"})
        assert r.headers["location"] == "/trading"
    finally:
        with SessionLocal() as s:
            risk.set_paused(s, False)


@respx.mock
async def test_flash_after_a_redirect_shows_in_the_page_frame(api):
    respx.get(url__startswith=f"{BASE}/").mock(return_value=Response(200, json=[]))
    r = await api.get("/login", headers=HTML)  # login off: to the overview, with a message
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert flashes_from_response(r) == [{"kind": "info", "text": LOGIN_OFF_MESSAGE}]
    page = await api.get("/", headers=HTML)
    main = page.text.split('id="main"', 1)[1]
    assert 'notice notice-info" role="status"' in main and LOGIN_OFF_MESSAGE in main
    assert LOGIN_OFF_MESSAGE not in (await api.get("/", headers=HTML)).text  # shown once


async def test_flash_shows_on_the_login_layout_too(web):  # noqa: F811
    """Every HTML page consumes the flash, so the login layout must show it as well."""
    r = await web.put("/login", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    (message,) = flashes_from_response(r)
    page = await web.get("/login", headers=HTML)
    assert message["text"] in page.text and "auth-card" in page.text
    assert message["text"] not in (await web.get("/login", headers=HTML)).text


async def test_error_page_sits_in_the_frame_without_grabbing_focus(api):
    r = await api.get("/bestaat-niet", headers=HTML)
    assert r.status_code == 404 and 'id="main"' in r.text and "Hoofdnavigatie" in r.text
    # The page is the message: no alert role, so app.js does not move the focus to it.
    assert 'notice notice-error">' in r.text and 'role="alert"' not in r.text
