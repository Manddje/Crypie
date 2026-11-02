"""The contracts between the foundations and the page packages: the bot status from
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
    assert "Laatste cyclus " in html and " · volgende " in html and "laatst gelukt" in html
    assert "(elke " in html and "is-bad" not in html

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
    assert '<span class="is-bad">Bewaking stop-loss/take-profit:' in html
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


# -- Round 2: the page packages against each other ---------------------------------------------


async def test_strategy_page_points_to_the_proposal_handelen_shows(api):
    """Strategie links to Handelen's #voorstel only when Handelen features that proposal: the
    newest unapplied one, and only when it can be applied. An older applicable proposal behind
    a newer refused one is in the evaluation list, not at the top, so it is not linked."""
    from tests.test_ux_trading import _add_evaluation, _clean, _proposal

    _clean()
    try:
        _add_evaluation(_proposal(name="Ouder en goed", stop_loss_pct=4))
        _add_evaluation(_proposal(name="Zonder stop", stop_loss_pct=None, trailing_stop_pct=None))
        trading = (await api.get("/trading", headers=HTML)).text
        strategy = (await api.get("/strategy", headers=HTML)).text
        assert 'id="voorstel"' not in trading
        assert 'href="/trading#voorstel"' not in strategy and "Ouder en goed" not in strategy

        ev_id = _add_evaluation(_proposal(name="Nieuw en goed", stop_loss_pct=3))
        trading = (await api.get("/trading", headers=HTML)).text
        strategy = (await api.get("/strategy", headers=HTML)).text
        assert "Voorstel van Claude: Nieuw en goed" in trading
        assert 'href="/trading#voorstel"' in strategy and "Nieuw en goed" in strategy

        # Applied on Handelen: no longer waiting, and the strategy page offers the old one back.
        r = await api.post("/trading/action", data={"action": f"apply:{ev_id}"}, headers=HTML)
        assert r.status_code == 303 and r.headers["location"] == "/trading"
        strategy = (await api.get("/strategy", headers=HTML)).text
        assert 'href="/trading#voorstel"' not in strategy
        assert "Vorige strategie terugzetten" in strategy
    finally:
        _clean()


def test_a_refused_key_has_one_reason_on_every_page():
    """Overzicht (order card, balance notice, charts) names the same reason for a refused key
    as the footer and Instellingen (KEY_PROBLEMS_NL), and stays general for an unknown code."""
    from app.bitvavo.client import BitvavoError
    from app.main import KEY_PROBLEMS_NL, _bitvavo_problem

    text = _bitvavo_problem(BitvavoError(403, {"errorCode": 301, "error": "API Key length."}))
    assert text == f"Bitvavo weigert de API-sleutel (fout 403): {KEY_PROBLEMS_NL[301]}"
    text = _bitvavo_problem(BitvavoError(403, {"errorCode": 399, "error": "?"}))
    assert text.startswith("Bitvavo weigert de API-sleutel (fout 403); controleer de sleutel")
