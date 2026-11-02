"""The settings page after the UX audit: decimal comma, feedback in the block, live checklist,
key status, market check, unknown models, reset rules and the labels and descriptions of fields
(UX-03, -04, -08, -48 .. -60, -68, -73)."""

import html
import re

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app import flash
from app import main as main_module
from app import settings_store as cfg
from app.bitvavo.client import BitvavoClient, BitvavoError
from app.db import SessionLocal, init_db
from app.main import app
from tests.test_auth import PASSWORD
from tests.test_security_auth import web  # noqa: F401  (fixture)
from tests.test_security_stepup import owner  # noqa: F401  (fixture)

BASE = "https://api.bitvavo.com/v2"
HTML = {"accept": "text/html"}


def _forget_settings():
    with SessionLocal() as db:
        cfg.reset_overrides(db)


@pytest.fixture
async def api():
    init_db()
    _forget_settings()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    _forget_settings()


def _block(html: str, key: str) -> str:
    """The HTML of one settings block (its <section>, folded or not)."""
    start = re.search(rf'<section class="card settings-block[^"]*" id="{key}"', html).start()
    return html[start : html.index("</section>", start)]


# -- UX-03: decimal comma, no guessing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [("2,5", 2.5), ("2.5", 2.5), ("1000", 1000.0), ("0,125", 0.125), (" 25 ", 25.0), ("1,0", 1.0)],
)
def test_numbers_accept_a_decimal_comma(text, value):
    assert cfg.parse_decimal(text) == value


@pytest.mark.parametrize("text", ["1.000", "2,500", "1.000,50", "1,000.5", "10.000"])
def test_numbers_that_could_hold_a_thousands_separator_are_refused(text):
    with pytest.raises(ValueError, match="onduidelijk"):
        cfg.parse_decimal(text)


def test_form_text_reads_back_and_never_uses_an_exponent():
    for value in (2.5, 0.25, 1000.0, 1e7, 1234.567, 0.1, 6):
        text = cfg.number_text(value)
        assert "e" not in text and "." not in text
        assert cfg.parse_decimal(text) == pytest.approx(value)
    assert cfg.number_text(2.5) == "2,5" and cfg.number_text(1e7) == "10000000"


def test_values_are_shown_in_dutch_with_their_unit():
    spec = cfg.SPEC_BY_KEY
    assert cfg.display_value(spec["max_trade_eur"], 1000.0) == "€ 1.000"
    assert cfg.display_value(spec["daily_loss_limit_pct"], 2.5) == "2,5%"
    assert cfg.display_value(spec["claude_monthly_budget_usd"], 12.5) == "$ 12,50"
    assert cfg.display_value(spec["max_trades_per_day"], 6) == "6 orders"
    errors = cfg.check_ceilings({"max_trade_eur": 5000.0}, _env())
    assert "maximaal € 1.000" in errors["max_trade_eur"]


def _env():
    from app.config import Settings

    return Settings(_env_file=None)


async def test_a_comma_in_a_limit_is_saved_as_a_decimal_and_old_and_new_are_shown(api):
    form = {"group": "limits", "daily_loss_limit_pct": "2,5"}
    r = await api.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=limits#limits"
    assert app.state.settings.daily_loss_limit_pct == 2.5
    (message,) = flash.flashes_from_response(r)
    assert message["kind"] == "ok" and "Dagverlieslimiet: 5% → 2,5%" in message["text"]
    # The message appears in the block the browser jumps to, not at the top of the page.
    page = (await api.get("/settings?saved=limits", headers=HTML)).text
    assert "Dagverlieslimiet: 5% → 2,5%" in _block(page, "limits")
    main = page.split('id="main"', 1)[1].split("settings-layout", 1)[0]
    assert "Dagverlieslimiet: 5% → 2,5%" not in main
    # A reload later shows no stale confirmation.
    page = (await api.get("/settings?saved=limits", headers=HTML)).text
    assert "opgeslagen; het geldt direct" not in page


async def test_number_fields_are_text_fields_with_a_number_keyboard(api):
    page = (await api.get("/settings")).text
    tag = re.search(r'<input [^>]*id="f-daily_loss_limit_pct"[^>]*>', page).group(0)
    assert 'type="text"' in tag and 'inputmode="decimal"' in tag and 'value="5"' in tag
    tag = re.search(r'<input [^>]*id="f-trade_interval_min"[^>]*>', page).group(0)
    assert 'inputmode="numeric"' in tag


async def test_a_hard_limit_twice_as_loose_asks_once_more(api):
    form = {"group": "limits", "max_trade_eur": "500"}
    r = await api.post("/settings", data=form)
    assert r.status_code == 409
    block = _block(r.text, "limits")
    assert "meer dan twee keer zo ruim" in block and "Maximum per order: € 100 → € 500" in block
    assert 'name="confirm_loosen"' in block
    assert app.state.settings.max_trade_eur == 100.0
    r = await api.post("/settings", data=form | {"confirm_loosen": "1"}, follow_redirects=False)
    assert r.status_code == 303 and app.state.settings.max_trade_eur == 500.0
    # Stricter, or less than twice as loose, needs no extra step.
    r = await api.post(
        "/settings", data={"group": "limits", "max_trade_eur": "900"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_loosened_limits_follow_the_direction_of_each_limit():
    old = _env()
    looser = old.model_copy(
        update={"cooldown_min_after_stop": 60, "max_exposure_pct": 90.0, "live_capital_eur": 0.0}
    )
    texts = cfg.loosened_limits(old, looser)
    assert len(texts) == 1 and texts[0].startswith("Afkoelperiode na stop-loss")
    capital = old.model_copy(update={"live_capital_eur": 500.0})
    whole = cfg.loosened_limits(capital, capital.model_copy(update={"live_capital_eur": 0.0}))
    assert whole and "het hele Bitvavo-account" in whole[0]
    stricter = old.model_copy(update={"max_trade_eur": 20.0, "cooldown_min_after_stop": 480})
    assert cfg.loosened_limits(old, stricter) == []


# -- UX-04: a reset never switches to live ---------------------------------------------------------


async def test_block_reset_keeps_the_mode_and_a_reset_to_live_is_refused(api, monkeypatch):
    env = app.state.env
    live_env = env.model_copy(update={"trading_mode": "live"})
    monkeypatch.setenv("TRADING_MODE", "live")  # what the stored values fall back to
    monkeypatch.setattr(app.state, "env", live_env)
    monkeypatch.setattr(main_module, "live_blocker", lambda *a, **k: None)
    with SessionLocal() as db:
        # The portal holds paper mode against an environment that says live.
        cfg.save_overrides(db, {"trading_mode": "paper", "trade_interval_min": 30}, live_env)
    app.state.settings = app.state.settings.model_copy(update={"trade_interval_min": 30})
    r = await api.post(
        "/settings", data={"group": "trading", "action": "reset"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert app.state.broker.mode == "paper" and app.state.settings.trading_mode == "paper"
    with SessionLocal() as db:
        assert cfg.load_overrides(db) == {"trading_mode": "paper"}
    # Resetting everything (or the mode itself) through the API would switch to live: refused.
    for path in ("/api/settings", "/api/settings/trading_mode"):
        r = await api.delete(path)
        assert r.status_code == 409 and "TRADING_MODE=live" in r.json()["detail"]["trading_mode"]
    assert app.state.broker.mode == "paper"


async def test_reset_confirmation_names_what_goes_back(api):
    await api.put("/api/settings", json={"trade_interval_min": 30})
    page = (await api.get("/settings")).text
    button = re.search(r'<button [^>]*value="reset"[^>]*>', _block(page, "trading")).group(0)
    assert "Interval tussen cycli: 30 minuten → 60 minuten" in button
    assert "Aangepast. Standaard: <strong>60 minuten</strong>" in page
    assert 'data-reset-field="trade_interval_min" data-default="60"' in page


# -- UX-51: everything live needs, at once ---------------------------------------------------------


async def test_live_checklist_lists_every_missing_requirement(api):
    page = (await api.get("/settings")).text
    block = _block(page, "trading")
    assert "Live is nog niet mogelijk" in block
    for text in ("De login staat aan", "Er is een Bitvavo-sleutel", "op de server bevestigd"):
        assert text in block
    assert "Kapitaal voor de bot staat op 0" in block and 'href="#f-live_capital_eur"' in block
    assert "Auth_enabled" not in page and "title=" not in re.search(
        r'<div class="seg-radio".*?</div>', block, re.S
    ).group(0)
    r = await api.put("/api/settings", json={"trading_mode": "live"})
    detail = r.json()["detail"]["trading_mode"]
    assert "AUTH_ENABLED" in detail and "Bitvavo-sleutel" in detail and "LIVE_TRADING_ACK" in detail


async def test_switching_to_live_in_the_form_needs_the_typed_confirmation(api, monkeypatch):
    ok = [dict(r, ok=True) for r in main_module.live_requirements(app.state.env, app.state.client)]
    monkeypatch.setattr(main_module, "live_requirements", lambda *a: ok)
    page = (await api.get("/settings")).text
    assert 'id="confirm_live"' in page and "Live is mogelijk" in page
    r = await api.post("/settings", data={"group": "trading", "trading_mode": "live"})
    assert r.status_code == 400 and "Typ LIVE" in r.text
    assert re.search(r'<input [^>]*id="confirm_live"[^>]*aria-invalid="true"', r.text)
    assert app.state.broker.mode == "paper"


# -- UX-01: start afresh with the switch to live ---------------------------------------------------


@pytest.mark.parametrize("reset", [True, False])
async def test_the_switch_to_live_can_reset_the_paper_portfolio(api, monkeypatch, reset):
    from sqlalchemy import delete, select

    from app.db import Position

    ok = [dict(r, ok=True) for r in main_module.live_requirements(app.state.env, app.state.client)]
    monkeypatch.setattr(main_module, "live_requirements", lambda *a: ok)
    monkeypatch.setattr(main_module, "live_blocker", lambda *a, **k: None)
    with SessionLocal() as db:
        db.execute(delete(Position))
        db.add(
            Position(
                mode="paper",
                market="BTC-EUR",
                status="open",
                amount=0.001,
                entry_price=40000.0,
                entry_quote=40.1,
                stop_price=38000.0,
            )
        )
        db.commit()
    page = (await api.get("/settings")).text
    assert 'name="reset_paper_on_live"' in page and "1 open oefenpositie wordt gesloten" in page
    data = {"group": "trading", "trading_mode": "live", "confirm_live": "LIVE"}
    if reset:
        data["reset_paper_on_live"] = "1"
    r = await api.post("/settings", data=data, follow_redirects=False)
    assert r.status_code == 303 and app.state.broker.mode == "live"
    (msg,) = flash.flashes_from_response(r)
    with SessionLocal() as db:
        (pos,) = db.scalars(select(Position)).all()
        if reset:
            assert pos.status == "closed" and pos.close_reason == "reset"
            assert "1 open oefenpositie is gesloten zonder resultaat" in msg["text"]
        else:
            assert pos.status == "open" and "oefenportefeuille" not in msg["text"]
        db.execute(delete(Position))
        db.commit()


# -- UX-52: a key Bitvavo refuses is not 'ingesteld' -----------------------------------------------


@respx.mock
async def test_client_remembers_a_refused_key_until_a_signed_request_works():
    client = BitvavoClient("k" * 10, "s" * 10)
    respx.get(f"{BASE}/balance").mock(
        return_value=Response(403, json={"errorCode": 301, "error": "API Key must be of length 64"})
    )
    with pytest.raises(BitvavoError):
        await client.balance()
    assert client.key_problem["code"] == 301 and "length 64" in client.key_problem["error"]
    respx.get(f"{BASE}/account").mock(
        return_value=Response(200, json={"fees": {"taker": "0.0025"}})
    )
    await client.account()
    assert client.key_problem is None and client.key_ok_at
    await client.aclose()


async def test_refused_key_shows_on_the_settings_page_and_in_the_footer(api, monkeypatch):
    client = BitvavoClient("k" * 10, "s" * 10)
    client.key_problem = {
        "status": 403,
        "code": 301,
        "error": "API Key must be of length 64",
        "at": 0,
    }
    monkeypatch.setattr(app.state, "client", client)
    page = (await api.get("/settings")).text
    assert "geweigerd door Bitvavo" in _block(page, "omgeving")
    assert "de sleutel heeft niet de goede lengte" in page
    footer = page.split('<footer class="site">', 1)[1]
    assert "Bitvavo-sleutel ingesteld, maar geweigerd door Bitvavo" in footer
    other = (await api.get("/journal")).text.split('<footer class="site">', 1)[1]
    assert "geweigerd door Bitvavo" in other
    await client.aclose()


@respx.mock
async def test_connection_test_reports_the_answer_of_bitvavo(api, monkeypatch):
    client = BitvavoClient("k" * 10, "s" * 10)
    monkeypatch.setattr(app.state, "client", client)
    monkeypatch.setattr(main_module, "_last_bitvavo_test", 0.0)
    route = respx.get(f"{BASE}/account")
    route.mock(return_value=Response(200, json={"fees": {"taker": "0.0025"}}))
    r = await api.post("/settings/bitvavo-test", follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=omgeving#omgeving"
    (message,) = flash.flashes_from_response(r)
    assert message["kind"] == "ok" and "De sleutel werkt" in message["text"]
    assert "0,25%" in message["text"]
    monkeypatch.setattr(main_module, "_last_bitvavo_test", 0.0)
    route.mock(return_value=Response(403, json={"errorCode": 305, "error": "No active API key"}))
    r = await api.post("/settings/bitvavo-test", follow_redirects=False)
    message = flash.flashes_from_response(r)[-1]  # the first one is still pending in the jar
    assert message["kind"] == "error" and "Geweigerd door Bitvavo" in message["text"]
    assert "kent deze sleutel niet" in message["text"]
    await client.aclose()


# -- UX-08: the notification channel ---------------------------------------------------------------


async def test_notification_channel_and_test_button(api, monkeypatch):
    page = (await api.get("/settings")).text
    block = _block(page, "omgeving")
    assert "Meldingen" in block and "NOTIFY_URL" in block and "notify-test" not in block

    async def fake_send(settings):
        return True, "Testmelding verstuurd. Controleer je telefoon of de webhook."

    monkeypatch.setattr(main_module, "send_test", fake_send)
    monkeypatch.setattr(main_module, "notify_configured", lambda settings: True)
    assert 'action="/settings/notify-test#omgeving"' in (await api.get("/settings")).text
    r = await api.post("/settings/notify-test", follow_redirects=False)
    assert r.headers["location"] == "/settings?saved=omgeving#omgeving"
    page = (await api.get("/settings?saved=omgeving", headers=HTML)).text
    assert "Testmelding verstuurd" in _block(page, "omgeving")


# -- UX-53: markets that do not exist --------------------------------------------------------------


async def test_unknown_market_is_refused_with_a_suggestion(api, monkeypatch):
    async def known(client):
        return {"BTC-EUR", "ETH-EUR", "SOL-EUR", "BTC-USDC"}

    monkeypatch.setattr(main_module, "fetch_market_names", known)
    app.state.market_names = None
    r = await api.put("/api/settings", json={"markets": "BTC-EUR, BTCC-EUR"})
    assert r.status_code == 422
    assert r.json()["detail"]["markets"] == "BTCC-EUR bestaat niet op Bitvavo. Bedoel je BTC-EUR?"
    r = await api.put("/api/settings", json={"markets": "SOL-EUR"})
    assert r.status_code == 200


async def test_markets_are_saved_unchecked_when_bitvavo_does_not_answer(api):
    r = await api.put("/api/settings", json={"markets": "XYZ-EUR"})
    assert r.status_code == 200  # the conftest makes the list unavailable


# -- UX-60: unknown model names --------------------------------------------------------------------


async def test_unknown_model_is_saved_with_a_warning(api):
    form = {"group": "claude", "claude_model_news": "claude-haiku-5-5-typo"}
    r = await api.post("/settings", data=form, follow_redirects=False)
    assert r.status_code == 303
    kinds = [f["kind"] for f in flash.flashes_from_response(r)]
    assert kinds == ["ok", "warn"]
    page = (await api.get("/settings")).text
    warn = re.search(r'<p class="setting-warn" id="f-claude_model_news-warn"[^>]*>', page).group(0)
    assert "hidden" not in warn
    assert "claude_model_news" in (await api.get("/api/settings")).json()["warnings"]


# -- UX-54 / UX-73: labels, descriptions, errors ---------------------------------------------------


async def test_fields_have_one_label_and_their_help_unit_and_error_attached(api):
    page = (await api.get("/settings")).text
    # A switch: its name is the one label; the Aan/Uit text is hidden from screen readers.
    assert page.count('for="f-trading_enabled"') == 1
    assert '<label class="switch">' not in page
    assert re.search(r'<input type="checkbox" role="switch" id="f-trading_enabled"', page)
    assert '<span class="state" aria-hidden="true">' in page
    # Modus is a fieldset with a legend, not a label pointing at nothing.
    assert 'for="f-trading_mode"' not in page
    assert re.search(r'<fieldset class="setting[^"]*setting--group"[^>]*>\s*<legend', page)
    tag = re.search(r'<input [^>]*id="f-daily_loss_limit_pct"[^>]*>', page).group(0)
    described = re.search(r'aria-describedby="([^"]+)"', tag).group(1).split()
    assert described[:2] == ["f-daily_loss_limit_pct-unit", "f-daily_loss_limit_pct-help"]
    for ident in described:
        assert f'id="{ident}"' in page, ident

    r = await api.post(
        "/settings",
        data={"group": "trading", "markets": "BTC-USD", "trade_interval_min": "0"},
    )
    assert r.status_code == 400
    block = _block(r.text, "trading")
    assert "2 velden kloppen niet" in block
    assert '<a href="#f-markets" data-focus-link>Markten</a>' in block
    assert '<a href="#f-trade_interval_min" data-focus-link>Interval tussen cycli</a>' in block
    tag = re.search(r'<input [^>]*id="f-markets"[^>]*>', block).group(0)
    assert 'aria-invalid="true"' in tag and "f-markets-error" in tag
    # Edited but not saved: marked, also after the error.
    assert re.search(r'class="setting has-error is-changed[^"]*" id="row-markets"', block)
    r = await api.post(
        "/settings", data={"group": "bescherming", "event_calendar": "2026-13-45 nope"}
    )
    assert r.status_code == 400
    block = _block(r.text, "bescherming")
    assert '<a href="#f-event_calendar" data-focus-link>Agenda</a>' in block


async def test_fields_under_a_switch_are_dimmed_while_it_is_off(api):
    await api.put("/api/settings", json={"news_enabled": False, "gdelt_enabled": False})
    page = (await api.get("/settings")).text
    row = re.search(r'<div class="setting[^"]*" id="row-news_fetch_interval_min"', page).group(0)
    assert "is-off" in row
    assert "Geldt alleen als 'Nieuws automatisch ophalen' aan staat." in html.unescape(page)
    assert re.search(r'id="row-gdelt_query" data-setting="gdelt_query" data-depends-on', page)
    # The value is still in the form and still saved.
    assert re.search(
        r'id="f-news_fetch_interval_min" name="news_fetch_interval_min" value="\d+"', page
    )


# -- UX-56 / UX-57: navigation and blocks ----------------------------------------------------------


async def test_settings_page_has_a_list_of_parts_and_sensible_blocks(api):
    page = (await api.get("/settings")).text
    nav = re.search(r'<nav class="settings-toc".*?</nav>', page, re.S).group(0)
    for key in (
        "trading",
        "bescherming",
        "limits",
        "costs",
        "claude",
        "news",
        "weergave",
        "oefenportefeuille",
        "omgeving",
    ):
        assert f'href="#{key}"' in nav
    assert "segmented" not in nav  # a list of links, not tabs
    # UX-56: every block (and Weergave on its own, UX-57) folds to a summary with scripts on.
    blocks = len(cfg.GROUPS) + len(cfg.OWN_FORM_PARTS) + 2
    assert page.count("data-block-toggle") == blocks
    assert page.count('class="block-summary"') == blocks
    assert "Alles openklappen" in nav
    costs = _block(page, "costs")
    assert "Handelskosten per order" in costs and "Handelskosten in backtests" in costs
    assert "Handelskosten" not in _block(page, "limits") and "backtests" not in _block(
        page, "claude"
    )
    paper = _block(page, "oefenportefeuille")
    assert 'name="paper_start_eur"' in paper and 'value="reset_paper"' in paper
    assert 'name="paper_start_eur"' not in _block(page, "trading")
    assert "Maximum aantal orders per dag" in page and ">orders<" in page
    assert ">Weergave<" in _block(page, "weergave") and "decision_history_days" not in _block(
        page, "trading"
    )
    assert 'name="part" value="weergave"' in _block(page, "weergave")
    assert 'name="current_password"' not in _block(page, "weergave")
    assert 'class="mono">MAX_TRADE_EUR_CEILING' in page and "€ 1.000" in page


def test_the_fee_moved_but_still_asks_for_the_password():
    assert cfg.SPEC_BY_KEY["trading_fee_pct"].group == "costs"
    assert "trading_fee_pct" in cfg.SENSITIVE_KEYS
    assert all(cfg.group_has_sensitive(g) for g in cfg.GROUPS)


async def test_paper_reset_saves_a_new_start_amount_with_it(api):
    r = await api.post(
        "/settings",
        data={"action": "reset_paper", "paper_start_eur": "2500"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=paper"
    assert (await api.get("/api/balance")).json()["balances"] == {"EUR": 2500.0}
    page = (await api.get("/settings?saved=paper", headers=HTML)).text
    assert "Het saldo staat weer op € 2.500" in _block(page, "oefenportefeuille")
    r = await api.post("/settings", data={"action": "reset_paper", "paper_start_eur": "abc"})
    assert r.status_code == 400 and "geen geldig getal" in _block(r.text, "oefenportefeuille")


# -- UX-48 / UX-55 / UX-62: the password in a block ------------------------------------------------


async def test_password_fields_and_locks_say_what_needs_them(owner):  # noqa: F811
    page = (await owner.get("/settings")).text
    limits = _block(page, "limits")
    assert limits.count("vraagt wachtwoord") == len(cfg.keys_in_group("limits"))
    claude = _block(page, "claude")
    assert claude.count("vraagt wachtwoord") == 1 and "Nodig als je het maandbudget" in claude
    assert "vraagt wachtwoord" not in re.search(
        r'id="row-trade_interval_min".*?</div>\s*</div>', page, re.S
    ).group(0)


async def test_failed_password_is_shown_in_the_block_with_the_field_marked(owner):  # noqa: F811
    form = {"group": "limits", "max_trade_eur": "40", "current_password": "fout-wachtwoord"}
    r = await owner.post("/settings", data=form)
    assert r.status_code == 403
    block = _block(r.text, "limits")
    assert "Niet opgeslagen:" in block and "Het wachtwoord klopt niet" in block
    assert re.search(r'<input type="password" id="pw-limits"[^>]*aria-invalid="true"', block)
    assert re.search(r'class="setting is-changed" id="row-max_trade_eur"', block)
    assert "Nog 4 pogingen" not in block  # only shown when few are left
    for _ in range(2):
        r = await owner.post("/settings", data=form)
    assert "Nog 2 pogingen" in _block(r.text, "limits")
    r = await owner.post("/settings", data=form | {"current_password": PASSWORD})
    assert app.state.settings.max_trade_eur == 40.0
