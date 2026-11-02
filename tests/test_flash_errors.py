"""One-time messages (app/flash.py), error pages and form addresses (app/errors.py), static
caching and compression (UX-16, UX-99, UX-101, UX-103)."""

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from httpx import ASGITransport, AsyncClient

from app import flash
from app.bitvavo.client import BitvavoNetworkError
from app.errors import LOGIN_OFF_MESSAGE, page_for
from app.flash import (
    FlashMiddleware,
    add_flash,
    decode,
    encode,
    flashes_from_response,
    redirect_with_flash,
)
from app.main import app
from tests.test_auth import _setup
from tests.test_security_auth import web  # noqa: F401 - the login-on fixture

HTML = {"accept": "text/html,application/xhtml+xml"}


# -- flash cookie ------------------------------------------------------------------------------


def test_flash_cookie_round_trip_and_tampering():
    value = encode([{"kind": "ok", "text": "Order geplaatst"}, {"kind": "raar", "text": "x"}])
    assert decode(value) == [
        {"kind": "ok", "text": "Order geplaatst"},
        {"kind": "info", "text": "x"},  # an unknown kind becomes info
    ]
    payload, _, mac = value.partition(".")
    assert decode(payload + "." + mac[:-2] + "AA") == []  # forged signature
    assert decode("garbage") == [] and decode(None) == [] and decode("") == []
    assert decode(value, now=time.time() + flash.MAX_AGE_S + 5) == []  # expired
    # Another key (another instance with another secret) does not accept it.
    flash.configure("een ander geheim dat lang genoeg is voor de test")
    try:
        assert decode(value) == []
    finally:
        flash.configure(None)


def test_flash_cookie_stays_small():
    many = [{"kind": "error", "text": f"{i} " + "€" * 390} for i in range(10)]
    value = encode(many)
    assert len(value) <= flash.MAX_COOKIE_BYTES
    kept = decode(value)
    assert kept and kept[-1]["text"].startswith("9 ")  # the newest survive


def _mini_app() -> FastAPI:
    mini = FastAPI()
    mini.add_middleware(FlashMiddleware)

    @mini.post("/act")
    async def act():
        return redirect_with_flash("/page", "ok", "Gelukt")

    @mini.post("/two")
    async def two():
        response = redirect_with_flash("/page", "ok", "Een")
        return add_flash(response, "warn", "Twee")

    @mini.get("/page")
    async def page(request: Request):
        return HTMLResponse(repr(request.state.flashes))

    @mini.get("/data")
    async def data(request: Request):
        return JSONResponse({"flashes": request.state.flashes})

    @mini.get("/hop")
    async def hop():
        return redirect_with_flash("/page", "info", "Nog een")

    return mini


@pytest.fixture
async def mini():
    transport = ASGITransport(app=_mini_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_flash_survives_redirects_and_json_until_a_page_shows_it(mini):
    r = await mini.post("/act")
    assert r.status_code == 303 and "x-crypie-flash" not in r.headers
    assert flashes_from_response(r) == [{"kind": "ok", "text": "Gelukt"}]
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Max-Age=120" in cookie
    # JSON (e.g. a chart fetch) does not consume it.
    r = await mini.get("/data")
    assert r.json() == {"flashes": [{"kind": "ok", "text": "Gelukt"}]}
    assert "set-cookie" not in r.headers
    # A redirect adds to it instead of dropping the pending one.
    r = await mini.get("/hop")
    assert [f["text"] for f in flashes_from_response(r)] == ["Gelukt", "Nog een"]
    # The page shows both and clears the cookie.
    r = await mini.get("/page")
    assert "Gelukt" in r.text and "Nog een" in r.text
    assert "Max-Age=0" in r.headers["set-cookie"]
    r = await mini.get("/page")
    assert r.text == "[]"


async def test_several_flashes_and_a_forged_cookie(mini):
    r = await mini.post("/two")
    assert [f["kind"] for f in flashes_from_response(r)] == ["ok", "warn"]
    mini.cookies.clear()
    mini.cookies.set("crypie_flash", "eyJmIjpbXX0.bogus")
    r = await mini.get("/data")
    assert r.json() == {"flashes": []} and "Max-Age=0" in r.headers["set-cookie"]


async def test_flash_cookie_uses_the_host_prefix_over_https():
    transport = ASGITransport(app=_mini_app())
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        r = await c.post("/act")
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-crypie_flash=") and "Secure" in cookie
        assert "Gelukt" in (await c.get("/page")).text


# -- error pages and form addresses (login off: the test default) ---------------------------


@pytest.fixture
async def api():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_unknown_page_is_an_html_page_with_a_way_back(api):
    r = await api.get("/bestaat-niet", headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Pagina niet gevonden" in r.text and 'href="/"' in r.text and "Naar overzicht" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    # The API and clients that do not ask for HTML keep JSON.
    assert (await api.get("/api/bestaat-niet", headers=HTML)).json() == {"detail": "Not Found"}
    assert (await api.get("/bestaat-niet")).json() == {"detail": "Not Found"}


@pytest.mark.parametrize(
    "path,target",
    [
        ("/orders", "/"),
        ("/trading/action", "/trading"),
        ("/news/refresh", "/news"),
        ("/journal/7/note", "/journal#trade-7"),
    ],
)
async def test_get_on_a_form_address_goes_to_its_page(api, path, target):
    r = await api.get(path)
    assert r.status_code == 303 and r.headers["location"] == target
    assert page_for(app, path) == target
    # Other methods keep the 405.
    assert (await api.put(path)).status_code == 405


@pytest.mark.parametrize(
    "path", ["/login", "/setup", "/account", "/login/mfa", "/login/mfa/setup", "/account/mfa"]
)
async def test_login_pages_redirect_to_the_overview_when_login_is_off(api, path):
    r = await api.get(path, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert flashes_from_response(r) == [{"kind": "info", "text": LOGIN_OFF_MESSAGE}]
    r = await api.post(path, data={"username": "x", "password": "y", "action": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/"


async def test_get_logout_with_login_off(api):
    r = await api.get("/logout", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/"


async def test_refused_form_post_goes_back_to_its_page_with_a_message(api):
    r = await api.post("/settings", data={"group": "onbekend"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert flashes_from_response(r) == [{"kind": "error", "text": "Onbekende groep."}]
    r = await api.post("/journal/999/note", data={"note": "x"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/journal#trade-999"
    assert flashes_from_response(r)[0]["kind"] == "error"
    # Without HTML in Accept (scripts, the existing API clients) the status stays.
    assert (await api.post("/settings", data={"group": "onbekend"})).status_code == 400


async def test_invalid_form_input_is_explained(api):
    r = await api.post(
        "/orders", data={"market": "BTC-EUR", "side": "buy", "amount_quote": "abc"}, headers=HTML
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert flashes_from_response(r) == [
        {"kind": "error", "text": "Niet verwerkt: bedrag is geen geldig getal."}
    ]
    r = await api.get("/news?hours=0", headers=HTML)
    assert r.status_code == 400 and "Ongeldig adres" in r.text and 'href="/news"' in r.text
    assert (await api.get("/news?hours=0")).status_code == 422


async def test_cycle_while_bitvavo_is_down(api, monkeypatch):
    async def down(markets):
        raise BitvavoNetworkError(TimeoutError("read timed out"))

    monkeypatch.setattr(app.state.engine.client, "ticker_prices", down)
    r = await api.post("/trading/action", data={"action": "cycle"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/trading"
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "error"
    assert msg["text"].startswith("Cyclus niet gedraaid: Bitvavo reageert niet")
    assert "probeert het later opnieuw" in msg["text"]
    assert app.state.engine.cycle_error is not None
    r = await api.post("/api/trading/cycle")
    assert r.status_code == 502 and "detail" in r.json()


async def test_a_crash_shows_the_500_page_with_the_security_headers(api, monkeypatch):
    from app import main

    def boom(session):
        raise RuntimeError("geheim intern detail")

    monkeypatch.setattr(main, "load_plan", boom)
    r = await api.get("/plan", headers=HTML)
    assert r.status_code == 500 and "Er ging iets mis" in r.text
    assert "geheim intern detail" not in r.text and 'href="/plan"' in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    r = await api.get("/api/plan", headers=HTML)
    assert r.status_code == 500 and r.json() == {"detail": "Interne fout"}


# -- static files: caching and compression ----------------------------------------------------


async def test_versioned_static_files_are_cached_and_compressed(api):
    r = await api.get(
        "/static/vendor/lightweight-charts.standalone.production.js?v=5.2.1",
        headers={"accept-encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-encoding"] == "gzip" and "Accept-Encoding" in r.headers["vary"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert len(r.content) > 100_000  # httpx decompresses: the full file arrived
    assert (await api.get("/static/app.css")).headers["cache-control"] == "no-cache"
    missing = await api.get("/static/bestaat-niet.js?v=1")
    assert missing.status_code == 404 and "immutable" not in missing.headers.get(
        "cache-control", ""
    )


async def test_pages_are_compressed(api):
    r = await api.get("/plan", headers={**HTML, "accept-encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"


# -- with the login on ------------------------------------------------------------------------


async def test_login_pages_with_secrets_are_not_compressed(web):  # noqa: F811
    r = await web.get("/login", headers={**HTML, "accept-encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers
    assert r.headers["cache-control"] == "no-store"


async def test_error_pages_for_visitors_without_a_login_show_no_details(web):  # noqa: F811
    r = await web.get("/static/bestaat-niet.css", headers=HTML)
    assert r.status_code == 404 and "Pagina niet gevonden" in r.text
    assert "Markten:" not in r.text and "Hoofdnavigatie" not in r.text
    # Not logged in: GET /logout is just the login page.
    r = await web.get("/logout", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_get_logout_shows_a_logout_button(web):  # noqa: F811
    await _setup(web)
    r = await web.get("/logout", headers=HTML)
    assert r.status_code == 200 and 'action="/logout"' in r.text and "Uitloggen" in r.text
    assert (await web.get("/bestaat-niet", headers=HTML)).status_code == 404
