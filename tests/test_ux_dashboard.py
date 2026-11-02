"""The overview page (Overzicht): order form, hero, open positions and Bitvavo outages.

UX-01 orders per mode, UX-02/03/19/20/22 the manual order, UX-06/09/25 the hero, UX-17 'Aan de
slag', UX-26 net results, UX-99 messages after an order, UX-102 a slow or unreachable Bitvavo.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app import flash, main
from app.bitvavo.broker import PAPER_WALLET_KEY, OrderUncertain, recent_orders
from app.bitvavo.client import BitvavoError, BitvavoNetworkError
from app.db import Decision, Order, Position, SessionLocal, Setting, init_db
from app.flash import flashes_from_response
from app.main import (
    GETTING_STARTED_KEY,
    SELL_ALL_EUR,
    _order_problem_nl,
    _portfolio,
    _position_rows,
    app,
    parse_amount_eur,
)
from app.trading import risk
from app.trading.plan import PLAN_KEY

HTML = {"accept": "text/html,application/xhtml+xml"}
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
TOUCHED_KEYS = (PAPER_WALLET_KEY, risk.PAUSE_KEY, GETTING_STARTED_KEY, PLAN_KEY)
PRICES = {"BTC-EUR": 40000.0, "ETH-EUR": 2000.0}


def _snapshot():
    with SessionLocal() as s:
        return {
            "orders": set(s.scalars(select(Order.id))),
            "positions": set(s.scalars(select(Position.id))),
            "decisions": set(s.scalars(select(Decision.id))),
            "settings": {
                k: (row.value if (row := s.get(Setting, k)) else None) for k in TOUCHED_KEYS
            },
        }


def _restore(snap):
    with SessionLocal() as s:
        s.execute(delete(Order).where(Order.id.not_in(snap["orders"])))
        s.execute(delete(Position).where(Position.id.not_in(snap["positions"])))
        s.execute(delete(Decision).where(Decision.id.not_in(snap["decisions"])))
        for key, value in snap["settings"].items():
            s.execute(delete(Setting).where(Setting.key == key))
            if value is not None:
                s.add(Setting(key=key, value=value))
        s.commit()


@pytest.fixture
async def api(monkeypatch):
    init_db()
    snap = _snapshot()
    calls = {"prices": 0}
    async with app.router.lifespan_context(app):

        async def prices(markets):
            calls["prices"] += 1
            return {m: PRICES[m] for m in markets if m in PRICES}

        async def price(market):
            return PRICES[market]

        for client in {app.state.client, app.state.engine.client}:
            monkeypatch.setattr(client, "ticker_prices", prices)
            monkeypatch.setattr(client, "ticker_price", price)
        app.state.engine.last_prices = {}
        app.state.engine.last_prices_at = None
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.calls = calls
            yield c
    _restore(snap)


def _clear_trades():
    with SessionLocal() as s:
        s.execute(delete(Order))
        s.execute(delete(Position))
        s.execute(delete(Setting).where(Setting.key == PAPER_WALLET_KEY))
        s.commit()


def _order_card(page: str) -> str:
    start = page.index('id="order"')
    return page[start : page.index("</section>", start)]


def _add_position(market="BTC-EUR", mode="paper", amount=0.001, entry_price=40000.0):
    with SessionLocal() as s:
        p = Position(
            mode=mode,
            market=market,
            status="open",
            amount=amount,
            entry_price=entry_price,
            entry_quote=amount * entry_price * 1.0025,
            stop_price=entry_price * 0.95,
        )
        s.add(p)
        s.commit()
        return p.id


# -- the amount as typed (UX-03) ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,value",
    [("25", 25.0), ("25,50", 25.5), ("25.5", 25.5), (" € 1000 ", 1000.0), ("0,5", 0.5)],
)
def test_amounts_accept_a_decimal_comma(text, value):
    assert parse_amount_eur(text) == value


@pytest.mark.parametrize(
    "text,message",
    [
        ("1.000", "onduidelijk"),
        ("2,500", "onduidelijk"),
        ("1.000,50", "onduidelijk"),
        ("25,", "geen geldig getal"),
        (",5", "geen geldig getal"),
        ("0,125", "twee cijfers"),
        ("abc", "geen geldig getal"),
        ("inf", "geen geldig getal"),
        ("-5", "geen geldig getal"),
        ("1e3", "geen geldig getal"),
        ("", "vul een bedrag in"),
    ],
)
def test_unclear_or_invalid_amounts_are_refused(text, message):
    with pytest.raises(ValueError, match=message):
        parse_amount_eur(text)


def test_engine_refusals_read_as_dutch():
    assert _order_problem_nl("maximaal € 100.00 per order (maximum per order)") == (
        "maximaal € 100,00 per order"
    )
    assert "noodstop" in _order_problem_nl("handelen is gepauzeerd (kill switch); kopen kan niet")
    assert _order_problem_nl("maximum van 6 trades per dag bereikt") == (
        "maximum van 6 orders per dag bereikt"
    )
    assert _order_problem_nl("blootstellingslimiet van 50.0% bereikt").endswith("50% bereikt")
    assert "2,5%" in _order_problem_nl("dagverlieslimiet van 2.5% bereikt")
    assert _order_problem_nl("bedrag onder minimum van 10.0 EUR") == (
        "bedrag onder minimum van € 10,00"
    )


# -- the order form: refusals, PRG and the values kept (UX-02, UX-16, UX-19, UX-99) ----------


async def test_a_refused_order_goes_back_to_the_card_with_the_values(api):
    _clear_trades()
    form = {"market": "ETH-EUR", "side": "sell", "amount_quote": "25,50"}
    r = await api.post("/orders", data=form, headers=HTML)
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert url.path == "/" and url.fragment == "order"
    assert parse_qs(url.query) == {"market": ["ETH-EUR"], "side": ["sell"], "amount": ["25,50"]}
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "error"
    assert msg["text"] == "Order niet geplaatst: er is geen open positie in ETH-EUR om te verkopen."

    page = await api.get(r.headers["location"], headers=HTML)
    card = _order_card(page.text)
    assert "Order niet geplaatst: er is geen open positie in ETH-EUR" in card
    assert 'role="alert"' in card
    # Not shown twice (the top of the page has no copy) and never as 'niet bereikbaar'.
    assert page.text.count("Order niet geplaatst") == 1
    assert "niet bereikbaar" not in page.text
    # The form shows what was sent.
    assert '<option value="ETH-EUR" selected>' in card
    assert 'value="sell" checked' in card and 'value="buy" checked' not in card
    assert 'value="25,50"' in card


async def test_without_html_the_refusal_is_on_the_page_with_its_status(api):
    _clear_trades()
    r = await api.post("/orders", data={"market": "ETH-EUR", "side": "sell", "amount_quote": "10"})
    assert r.status_code == 400
    assert "Order niet geplaatst: er is geen open positie" in _order_card(r.text)


async def test_an_unclear_amount_places_nothing(api, monkeypatch):
    placed = []

    async def manual_order(session, market, side, amount_quote, reason=None):
        placed.append(amount_quote)

    monkeypatch.setattr(app.state.engine, "manual_order", manual_order)
    r = await api.post(
        "/orders", data={"market": "BTC-EUR", "side": "buy", "amount_quote": "1.000"}, headers=HTML
    )
    assert placed == []
    (msg,) = flashes_from_response(r)
    assert msg["text"].startswith("Order niet geplaatst: '1.000' is onduidelijk")
    # A comma amount reaches the engine as the number it means.
    r = await api.post(
        "/orders", data={"market": "BTC-EUR", "side": "buy", "amount_quote": "25,50"}, headers=HTML
    )
    assert placed == [25.5]


async def test_a_placed_order_says_what_happened_in_the_card(api):
    _clear_trades()
    r = await api.post(
        "/orders", data={"market": "ETH-EUR", "side": "buy", "amount_quote": "40"}, headers=HTML
    )
    assert r.status_code == 303 and r.headers["location"] == "/#order"
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "ok"
    assert msg["text"].startswith("Gekocht: 0,01995 ETH voor € 40,00 tegen € 2.000,00")
    assert "(oefengeld)" in msg["text"] and "kosten € 0,10" in msg["text"]

    page = (await api.get("/", headers=HTML)).text
    assert "Gekocht: 0,01995 ETH" in _order_card(page)
    assert 'role="status"' in _order_card(page)

    # Selling the whole position: the engine gets more than the position is worth.
    r = await api.post(
        "/orders",
        data={"market": "ETH-EUR", "side": "sell", "amount_quote": "", "sell_all": "1"},
        headers=HTML,
    )
    (msg,) = flashes_from_response(r)
    assert msg["text"].startswith("Verkocht: 0,01995 ETH") and "positie gesloten" in msg["text"]
    _clear_trades()


async def test_sell_all_hands_the_engine_the_whole_position(api, monkeypatch):
    seen = []

    async def manual_order(session, market, side, amount_quote, reason=None):
        seen.append((side, amount_quote))
        return SimpleNamespace(status="filled", amount=0.5, amount_quote=1000.0, price=2000.0)

    monkeypatch.setattr(app.state.engine, "manual_order", manual_order)
    form = {"market": "ETH-EUR", "side": "sell", "amount_quote": "", "sell_all": "1"}
    await api.post("/orders", data=form, headers=HTML)
    # 'Hele positie verkopen' only means something for a sell.
    form = {"market": "ETH-EUR", "side": "buy", "amount_quote": "20", "sell_all": "1"}
    await api.post("/orders", data=form, headers=HTML)
    assert seen == [("sell", SELL_ALL_EUR), ("buy", 20.0)]


@pytest.mark.parametrize(
    "exc,kind,text",
    [
        (
            BitvavoNetworkError(TimeoutError("read timed out")),
            "error",
            "Order niet geplaatst: Bitvavo reageert niet (geen verbinding of een time-out).",
        ),
        (
            BitvavoError(403, {"errorCode": 305, "error": "No active API key found."}),
            "error",
            "Order niet geplaatst: Bitvavo weigert de API-sleutel (fout 403)",
        ),
        (
            BitvavoError(400, {"errorCode": 216, "error": "Insufficient balance."}),
            "error",
            "Order niet geplaatst: Bitvavo weigerde de order (fout 400: Insufficient balance.)",
        ),
    ],
)
async def test_bitvavo_problems_each_have_their_own_text(api, monkeypatch, exc, kind, text):
    async def manual_order(*args, **kwargs):
        raise exc

    monkeypatch.setattr(app.state.engine, "manual_order", manual_order)
    form = {"market": "BTC-EUR", "side": "buy", "amount_quote": "20"}
    r = await api.post("/orders", data=form, headers=HTML)
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == kind and msg["text"].startswith(text)


async def test_an_order_with_an_unknown_outcome_is_not_called_refused(api, monkeypatch):
    async def manual_order(*args, **kwargs):
        raise OrderUncertain(SimpleNamespace(client_order_id="c1", market="BTC-EUR"), "timeout")

    monkeypatch.setattr(app.state.engine, "manual_order", manual_order)
    r = await api.post(
        "/orders", data={"market": "BTC-EUR", "side": "buy", "amount_quote": "20"}, headers=HTML
    )
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "warn"
    assert msg["text"].startswith("Order verstuurd, maar de uitkomst is nog niet bekend")
    assert "niet geplaatst" not in msg["text"]


# -- what the card tells before an order (UX-20, UX-22) -------------------------------------


async def test_the_help_text_follows_the_rules(api):
    _clear_trades()
    card = _order_card((await api.get("/")).text)
    assert "tellen niet mee" not in card
    assert "tellen mee voor het maximum aantal orders per dag (vandaag 0, maximum 6)" in card
    assert "Kopen: € 10,00 tot € 100,00 per order" in card
    assert "Verkopen kan vanaf € 5,00" in card
    assert 'data-min-buy="10.0" data-max-buy="100.0" data-min-sell="5.0"' in card
    assert 'inputmode="decimal"' in card and 'type="text"' in card
    assert "nog 1 order" not in card

    with SessionLocal() as s:
        for _ in range(5):
            s.add(
                Order(
                    mode="paper",
                    market="BTC-EUR",
                    side="buy",
                    order_type="market",
                    status="filled",
                    amount=0.001,
                    amount_quote=40.0,
                )
            )
        s.commit()
    card = _order_card((await api.get("/")).text)
    assert "Je kunt vandaag nog 1 order plaatsen (5 van maximaal 6 gebruikt)" in card
    with SessionLocal() as s:
        s.add(
            Order(
                mode="paper",
                market="BTC-EUR",
                side="buy",
                order_type="market",
                status="filled",
                amount=0.001,
                amount_quote=40.0,
            )
        )
        s.commit()
    card = _order_card((await api.get("/")).text)
    assert "Het maximum aantal orders per dag is bereikt: 6 orders vandaag (maximum 6)" in card
    assert "verkopen kan wel" in card
    _clear_trades()


async def test_the_sell_side_knows_the_position(api):
    _clear_trades()
    _add_position("BTC-EUR", amount=0.001)
    page = (await api.get("/?market=BTC-EUR&side=sell&amount=")).text
    card = _order_card(page)
    assert (
        'data-positions=\'{"BTC-EUR": {"amount": 0.001, "coin": "BTC", "value_eur": 40.0}}\''
        in card
    )
    assert "Open positie in BTC-EUR: 0,001 BTC ≈ € 40,00." in card
    assert 'name="sell_all"' in card and "Hele positie verkopen" in card
    _clear_trades()


# -- modes kept apart (UX-01, UX-05) ----------------------------------------------------------


def test_recent_orders_keep_the_modes_apart():
    with SessionLocal() as s:
        before = {o.id for o in recent_orders(s, 500)}
        s.add(
            Order(mode="paper", market="BTC-EUR", side="buy", order_type="market", status="filled")
        )
        s.add(
            Order(mode="live", market="ETH-EUR", side="buy", order_type="market", status="filled")
        )
        s.commit()
        new = [o for o in recent_orders(s, 500) if o.id not in before]
        assert {o.mode for o in new} == {"paper", "live"}
        assert all(o.mode == "live" for o in recent_orders(s, 500, mode="live"))
        assert all(o.mode == "paper" for o in recent_orders(s, 500, mode="paper"))
        s.execute(delete(Order).where(Order.id.in_([o.id for o in new])))
        s.commit()


async def test_live_shows_no_paper_orders_but_mentions_open_paper_positions(api, monkeypatch):
    _clear_trades()
    _add_position("BTC-EUR", mode="paper")
    with SessionLocal() as s:
        s.add(
            Order(
                mode="paper",
                market="BTC-EUR",
                side="buy",
                order_type="market",
                status="filled",
                amount=0.001,
                amount_quote=40.0,
                reason="manual: oefen",
            )
        )
        s.commit()

    class LiveStub:
        mode = "live"

        async def balances(self, session):
            return {"EUR": 500.0}

    monkeypatch.setattr(app.state.engine, "broker", LiveStub())
    monkeypatch.setattr(app.state, "broker", LiveStub())
    page = (await api.get("/")).text
    assert "Portefeuille (echt geld)" in page
    assert "Nog geen orders met echt geld" in page and "manual: oefen" not in page
    assert "Handmatig: oefen" not in page
    assert "Nog 1 open oefenpositie met fictief geld: BTC-EUR" in page
    assert "Echte order plaatsen" in page and "LIVE · echt geld: deze order plaatsen?" in page
    _clear_trades()


# -- the hero (UX-06, UX-09, UX-25, UX-26, UX-27) ---------------------------------------------


async def test_the_hero_has_the_status_and_the_kill_switch(api):
    page = (await api.get("/")).text
    assert "Bot actief" not in page and "Naar handelen ›" not in page
    assert 'class="status status-' in page
    hero = page[page.index('class="hero dash-hero"') : page.index('id="open-posities"')]
    assert 'action="/trading/action"' in hero and 'name="next" value="/"' in hero
    assert "Noodstop: kopen pauzeren" in hero and 'name="current_password"' not in hero
    # One message about the missing Claude key, which knows the configured mode.
    assert page.count("ANTHROPIC_API_KEY") <= 2  # hero and the 'Aan de slag' step
    assert "wacht de bot bij een koopsignaal af" in hero
    # The pause sentence is the kill switch's own; the old pause notice is gone.
    assert "Handelen staat op pauze" not in page

    with SessionLocal() as s:
        risk.set_paused(s, True)
    page = (await api.get("/")).text
    hero = page[page.index('class="hero dash-hero"') : page.index('id="open-posities"')]
    assert "Noodstop actief: kopen gepauzeerd" in hero and "Kopen hervatten" in hero
    assert 'data-confirm="Kopen hervatten? De bot mag dan weer kopen."' in hero
    with SessionLocal() as s:
        risk.set_paused(s, False)


async def test_the_status_block_can_refresh_itself(api):
    r = await api.get("/api/dashboard/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["level"] in ("ok", "warn", "error", "paused", "off") and body["paused"] is False
    assert "Noodstop: kopen pauzeren" in body["html"] and 'value="/"' in body["html"]


async def test_open_positions_show_a_net_result_right_under_the_hero(api):
    _clear_trades()
    _add_position("BTC-EUR", amount=0.001, entry_price=40000.0)
    page = (await api.get("/")).text
    # Under the hero, before the prices and the charts.
    assert page.index('id="open-posities"') < page.index('id="price-chart-card"')
    assert page.index('id="open-posities"') < page.index("Koersen</h2>")
    assert 'href="/trading#open-posities"' in page
    # 40 EUR now, 40.10 spent incl. the buy fee, 0.10 sell fee: -0.20 net (-0.5%).
    assert "Resultaat (netto)" in page and "-€ 0,20" in page and "-0,5%" in page
    assert "1 open positie" in page
    _clear_trades()


def test_position_rows_and_portfolio_helpers():
    p = SimpleNamespace(
        market="BTC-EUR",
        amount=0.001,
        entry_price=40000.0,
        entry_quote=40.1,
        stop_price=38000.0,
        take_profit_price=None,
    )
    view = _position_rows([p], {"BTC-EUR": 40000.0}, 0.25)
    assert view["rows"][0]["pnl_eur"] == pytest.approx(-0.2)
    assert view["pnl_pct"] == pytest.approx(-0.2 / 40.1 * 100)
    assert _position_rows([p], {}, 0.25)["pnl_eur"] is None
    # Without a price a coin counts at its entry price, and never silently as zero.
    folio = _portfolio({"EUR": 960.0, "BTC": 0.001}, {}, {"BTC-EUR": 40000.0})
    assert folio["total_eur"] == 1000.0 and folio["estimated"] == ["BTC"]
    folio = _portfolio({"EUR": 960.0, "BTC": 0.001}, {})
    assert folio["total_eur"] == 960.0 and folio["missing"] == ["BTC"]


async def test_zero_results_are_not_green(api):
    _clear_trades()
    page = (await api.get("/")).text
    assert '<div class="big-value is-md money zero">€ 0,00</div>' in page


# -- Bitvavo slow or down (UX-102) -------------------------------------------------------------


async def test_the_overview_renders_while_bitvavo_is_down(api, monkeypatch):
    _clear_trades()
    calls = {"n": 0}

    async def down(markets):
        calls["n"] += 1
        raise BitvavoNetworkError(TimeoutError("read timed out"))

    monkeypatch.setattr(app.state.client, "ticker_prices", down)
    app.state.engine.last_prices = {"BTC-EUR": 39000.0}
    app.state.engine.last_prices_at = datetime.now(UTC) - timedelta(minutes=10)
    page = (await api.get("/")).text
    assert "Koersen niet bijgewerkt:" in page and "Bitvavo reageert niet" in page
    assert "laatst bekende koersen van" in page
    assert "€ 39.000,00" in page  # the last known price
    assert "€ 1.000,00" in page  # the paper balance is local: still shown
    assert "Het oefensaldo staat in de app zelf en klopt gewoon." in page
    # The next view does not wait for Bitvavo again straight away.
    await api.get("/")
    assert calls["n"] == 1


async def test_prices_are_reused_for_a_few_seconds(api):
    await api.get("/")
    await api.get("/")
    assert api.calls["prices"] == 1


async def test_a_slow_bitvavo_does_not_hold_the_page(api, monkeypatch):
    import asyncio

    async def slow(markets):
        await asyncio.sleep(30)

    monkeypatch.setattr(main, "DASHBOARD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(app.state.client, "ticker_prices", slow)
    r = await api.get("/")
    assert r.status_code == 200 and "Bitvavo antwoordde niet binnen 0.05 seconden" in r.text


# -- first use (UX-17) -------------------------------------------------------------------------


async def test_getting_started_until_hidden(api):
    _clear_trades()
    with SessionLocal() as s:
        s.execute(delete(Setting).where(Setting.key.in_([GETTING_STARTED_KEY, PLAN_KEY])))
        s.commit()
    page = (await api.get("/")).text
    assert 'id="aan-de-slag"' in page and "0 van 5 stappen klaar" in page
    assert "Tradingplan schrijven" in page and 'href="/plan"' in page
    # Plain words first, the server variable as secondary text.
    assert 'Nu nog niet: de login staat uit. <span class="mono">AUTH_ENABLED</span>' in page

    r = await api.post("/dashboard/getting-started", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert 'id="aan-de-slag"' not in (await api.get("/")).text
    # A GET on the form address goes back to the overview.
    assert (await api.get("/dashboard/getting-started")).headers["location"] == "/"


# -- markup rules ------------------------------------------------------------------------------


def test_dashboard_markup_follows_the_rules():
    text = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    partial = (TEMPLATES / "_dashboard_status.html").read_text(encoding="utf-8")
    for source in (text, partial):
        assert 'style="' not in source
        assert not re.search(r"<script(?![^>]*\bsrc=)", source)
    assert 'role="group" aria-label="Markt"' in text and 'role="group" aria-label="Periode"' in text
    # The legend's swatches are hidden from screen readers, not the legend with its checkbox.
    assert 'class="chart-legend" aria-hidden' not in text
    assert "Winstkans" not in text and "Winnende trades" in text
    assert "'pos' if" not in text and "pnl_class" in text
    assert "/static/dashboard.js?v={{ asset_version }}" in text


async def test_a_refused_password_marks_the_field(api, monkeypatch):
    class LiveStub:
        mode = "live"

        async def balances(self, session):
            return {"EUR": 500.0}

    monkeypatch.setattr(app.state.engine, "broker", LiveStub())
    monkeypatch.setattr(main, "step_up_enabled", lambda request: True)
    page = (await api.get("/")).text
    assert 'name="current_password"' in page and 'aria-invalid="true"' not in page
    wrong = [{"kind": "error", "text": "Order niet geplaatst. Het wachtwoord klopt niet."}]
    api.cookies.set(flash.COOKIE_NAME, flash.encode(wrong))
    page = (await api.get("/", headers=HTML)).text
    assert "Het wachtwoord klopt niet" in _order_card(page)
    assert 'autocomplete="current-password" maxlength="1024" required aria-invalid="true"' in page
