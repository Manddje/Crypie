"""Follow-ups after the first verification round of the UX fixes: what the verifier still
found partial, plus the review findings (see docs/ux-audit.md, section Opvolging)."""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db import Decision, Evaluation, Position, SessionLocal, Setting, init_db
from app.main import APPLIED_PROPOSALS_KEY, app
from app.strategy.model import DEFAULT_STRATEGY
from app.strategy.store import PREVIOUS_KEY, STRATEGY_KEY
from app.trading import risk

HTML = {"accept": "text/html,application/xhtml+xml"}


def _clean():
    with SessionLocal() as s:
        for table in (Decision, Evaluation, Position):
            s.execute(delete(table))
        keys = (STRATEGY_KEY, APPLIED_PROPOSALS_KEY, PREVIOUS_KEY)
        s.execute(delete(Setting).where(Setting.key.in_(keys)))
        s.commit()
        risk.set_paused(s, False)


@pytest.fixture
async def api():
    init_db()
    _clean()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    _clean()


def _add_evaluation(proposal: dict) -> int:
    with SessionLocal() as s:
        ev = Evaluation(
            model="claude-fable-5-1",
            period_days=7,
            report_nl="Evaluatie",
            proposed_strategy=json.dumps(proposal),
        )
        s.add(ev)
        s.commit()
        return ev.id


# -- UX-12: the description warning also where proposals are applied ---------------------------


async def test_handelen_proposal_warns_when_only_the_rules_change(api):
    _add_evaluation(DEFAULT_STRATEGY.model_dump() | {"name": "Strakker", "stop_loss_pct": 4})
    page = (await api.get("/trading")).text
    assert "Wat er verandert als je dit voorstel toepast" in page
    assert "De regels veranderen, maar de omschrijving niet: na het toepassen" in page
    _clean()
    _add_evaluation(
        DEFAULT_STRATEGY.model_dump()
        | {"name": "Strakker", "stop_loss_pct": 4, "description": "Stop-loss 4%."}
    )
    page = (await api.get("/trading")).text
    assert "De regels veranderen, maar de omschrijving niet" not in page


# -- Review: limits keep their decimals ----------------------------------------------------------


def test_limit_percentages_keep_their_decimals():
    from app.web import pct_limit

    assert pct_limit(2.5) == "2,5%"
    assert pct_limit(5) == "5%"
    assert pct_limit(0.25) == "0,25%"
    assert pct_limit(None) == "–"


async def test_risk_views_state_the_enforced_decimal_limit(api, monkeypatch):
    from dataclasses import replace

    engine = app.state.engine
    monkeypatch.setattr(
        engine, "limits", replace(engine.limits, daily_loss_limit_pct=2.5, max_exposure_pct=62.5)
    )
    page = (await api.get("/trading")).text
    assert "grens -2,5%" in page and "grens -2%" not in page
    assert "62,5% van de portefeuille" in page
    assert "Bij 2,5% verlies op één dag" in page
    plan = (await api.get("/plan")).text
    assert "62,5% van de portefeuille" in plan


# -- UX-29: a mechanical exit keeps the price it hit ----------------------------------------------


def test_mechanical_exit_price_keeps_significant_digits():
    from app.trading import display
    from app.trading.engine import plain_number

    assert plain_number(4.056e-06) == "0.000004056"
    assert plain_number(66091.5) == "66091.5"
    d = Decision(
        mode="paper",
        market="SHIB-EUR",
        price=4.056e-06,
        signal="hold",
        source="mechanical",
        action="sell",
        reasoning=f"stop_loss geraakt op {plain_number(4.056e-06)}",
    )
    text, _ = display._explain(d, {})
    assert text == "Stop-loss geraakt op €\xa00,000004056"


# -- Review: the daily order maximum at 0, and counts past it ----------------------------------


def test_order_maximum_wording_at_zero_and_past_the_maximum():
    from app.labels import max_orders_rule_nl, orders_limit_nl

    assert "00:00 UTC" not in max_orders_rule_nl(0) and "niets gekocht" in max_orders_rule_nl(0)
    assert "00:00 UTC" not in orders_limit_nl(0, 0) and "staat op 0" in orders_limit_nl(0, 0)
    text = orders_limit_nl(7, 6)
    assert "7 van 6" not in text and "7 orders vandaag, maximum 6" in text


async def test_order_maximum_zero_never_promises_buying_comes_back(api, monkeypatch):
    from dataclasses import replace

    engine = app.state.engine
    monkeypatch.setattr(engine, "limits", replace(engine.limits, max_trades_per_day=0))
    page = (await api.get("/")).text
    assert "staat op 0" in page and "(0 van 0)" not in page
    trading = (await api.get("/trading")).text
    assert "Na 0 orders" not in trading and "Op 0 gezet" in trading
    assert "kopen kan weer vanaf 00:00 UTC" not in trading


# -- UX-01: paper positions that are still guarded while live ----------------------------------


class _LiveStub:
    mode = "live"

    async def balances(self, session):
        return {"EUR": 500.0}


def _paper_position() -> None:
    with SessionLocal() as s:
        s.add(
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
        s.commit()


async def test_live_shows_and_can_close_the_guarded_paper_positions(api, monkeypatch):
    _paper_position()
    monkeypatch.setattr(app.state.engine, "broker", _LiveStub())
    monkeypatch.setattr(app.state, "broker", _LiveStub())
    live_settings = app.state.settings.model_copy(update={"trading_mode": "live"})
    monkeypatch.setattr(app.state, "settings", live_settings)

    page = (await api.get("/trading")).text
    assert "Geen open posities in live" in page
    assert 'id="andere-modus"' in page and "Ook open in Oefenmodus" in page
    assert "Deze oefenpositie wordt nog bewaakt" in page and "BTC-EUR" in page
    overview = (await api.get("/")).text
    assert 'href="/trading#andere-modus"' in overview

    r = await api.get("/api/positions")
    assert r.json()["mode"] == "live" and r.json()["open"] == []
    r = await api.get("/api/positions?mode=paper")
    assert r.json()["mode"] == "paper" and len(r.json()["open"]) == 1
    assert (await api.get("/api/positions?mode=echt")).status_code == 422

    settings_page = (await api.get("/settings")).text
    assert 'value="reset_paper"' in settings_page and "Je handelt live" in settings_page
    assert "(BTC-EUR)" in settings_page and 'name="paper_start_eur"' not in settings_page
    r = await api.post(
        "/settings",
        data={"group": "paper", "action": "reset_paper", "paper_start_eur": "9999"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with SessionLocal() as s:
        (pos,) = s.query(Position).all()
        assert pos.status == "closed" and pos.close_reason == "reset"
    assert app.state.settings.paper_start_eur != 9999  # not changed while live


# -- UX-102 / UX-53: charts during an outage, and a refusal is not an outage --------------------


async def test_chart_apis_do_not_wait_again_after_an_outage(api, monkeypatch):
    import asyncio

    from app import main as main_module
    from app.bitvavo.client import BitvavoError

    calls = []

    async def hangs(market, interval, limit):
        calls.append(market)
        await asyncio.sleep(10)

    monkeypatch.setattr(main_module, "CHART_TIMEOUT_S", 0.05)
    monkeypatch.setattr(app.state.client, "candles", hangs)
    main_module._bitvavo_outages.clear()
    r = await api.get("/api/chart/BTC-EUR?period=7d")
    assert r.status_code == 502 and r.json()["reason"] == "unreachable"
    assert "antwoordde niet binnen" in r.json()["detail"]
    n = len(calls)
    # The next chart (and the portfolio chart) answer at once, without asking Bitvavo again.
    r = await api.get("/api/chart/ETH-EUR?period=7d")
    assert r.status_code == 502 and "net nog geprobeerd" in r.json()["detail"]
    r = await api.get("/api/portfolio/history?period=7d")
    assert r.status_code == 502 and r.json()["reason"] == "unreachable"
    assert len(calls) == n
    main_module._bitvavo_outages.clear()

    async def refuses(market, interval, limit):
        raise BitvavoError(400, {"errorCode": 205, "error": "market does not exist"})

    monkeypatch.setattr(app.state.client, "candles", refuses)
    r = await api.get("/api/chart/ETH-EUR?period=7d")
    body = r.json()
    assert r.status_code == 502 and body["reason"] == "refused"
    assert body["detail"] == (
        "Bitvavo weigerde het verzoek voor ETH-EUR (fout 400, code 205: market does not exist)"
    )
    assert not main_module._bitvavo_outages  # a refusal is no outage


def test_chart_js_offers_no_retry_for_a_refusal():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "app/static/chart.js").read_text(encoding="utf-8")
    assert 'err.reason === "refused"' in js and "Markten controleren" in js
    assert "/settings#row-markets" in js


async def test_saved_markets_bitvavo_does_not_list_are_flagged(api, monkeypatch):
    import time

    settings = app.state.settings.model_copy(update={"markets": ["BTC-EUR", "BTCC-EUR"]})
    monkeypatch.setattr(app.state, "settings", settings)
    app.state.market_names = (time.monotonic() + 600, {"BTC-EUR", "ETH-EUR"})
    page = (await api.get("/settings")).text
    assert 'id="f-markets-warn"' in page
    assert "Let op: BTCC-EUR bestaat niet op Bitvavo. Bedoel je BTC-EUR?" in page
    app.state.market_names = None


async def test_koersen_names_a_market_without_a_price(api, monkeypatch):
    from app import main as main_module

    async def prices(engine, client, markets):
        return {"BTC-EUR": 60000.0}, None, None

    monkeypatch.setattr(main_module, "_dashboard_prices", prices)
    page = (await api.get("/")).text
    assert '<span class="neg">geen koers</span>' in page
    assert "Bitvavo gaf geen koers voor ETH-EUR" in page


# -- Review: a failed save without JS does not mark untouched textareas ----------------------------


def test_textarea_line_ends_are_not_a_change():
    from app.main import _form_text

    assert _form_text("a\r\nb\r\n") == _form_text("a\nb")
    assert _form_text("a  \nb") == "a\nb"
    assert _form_text("a\nc") != _form_text("a\nb")


# -- UX-99: a message lands in the section the browser jumps to ----------------------------------


async def test_flashes_show_in_the_section_the_redirect_points_to(api):
    from app.flash import flashes_from_response

    r = await api.post("/plan", data={"goal": "Rustig groeien"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/plan#eigen"
    (f,) = flashes_from_response(r, with_at=True)
    assert f["at"] == "eigen"
    page = (await api.get("/plan", headers=HTML)).text
    section = page[page.index('id="eigen"') :]
    assert f["text"] in section  # in the section, not above it
    assert page.index(f["text"]) > page.index('id="eigen"')


def test_flash_anchor_is_kept_only_when_it_is_a_plain_id():
    from app.flash import _clean

    assert _clean([{"kind": "ok", "text": "x", "at": "berichten"}])[0]["at"] == "berichten"
    assert "at" not in _clean([{"kind": "ok", "text": "x", "at": "a b<script>"}])[0]


async def test_the_500_page_leaves_pending_messages_for_the_next_page(api, monkeypatch):
    from app import main as main_module

    r = await api.post("/trading/action", data={"action": "pause"}, headers=HTML)
    assert r.status_code == 303
    r = await api.post("/trading/action", data={"action": "resume"}, headers=HTML)
    cookie = r.headers["set-cookie"].split(";", 1)[0]
    text = "Kopen hervat"

    def crash(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(main_module, "_trading_context", crash)
    page = await api.get("/trading", headers=HTML | {"cookie": cookie})
    assert page.status_code == 500 and "mogelijk niet uitgevoerd" in page.text
    assert text not in page.text
    monkeypatch.undo()
    page = await api.get("/trading", headers=HTML | {"cookie": cookie})
    assert text in page.text


# -- Review: the manual order form ----------------------------------------------------------------


async def test_a_refused_amount_comes_back_as_typed(api):
    from urllib.parse import parse_qs, urlsplit

    for typed in ("1e3", "inf", "-5"):
        form = {"market": "BTC-EUR", "side": "buy", "amount_quote": typed}
        r = await api.post("/orders", data=form, headers=HTML)
        assert r.status_code == 303
        query = parse_qs(urlsplit(r.headers["location"]).query)
        assert query["amount"] == [typed], typed
    form = {"market": "BTC-EUR", "side": "buy", "amount_quote": "<b>1</b>"}
    r = await api.post("/orders", data=form, headers=HTML)
    assert "amount=&" in r.headers["location"] or r.headers["location"].endswith("amount=#order")


async def test_a_refused_order_does_not_use_up_its_form(api):
    form = {"market": "ETH-EUR", "side": "sell", "amount_quote": "10", "order_token": "tok-1"}
    r = await api.post("/orders", data=form, headers=HTML)
    from app.flash import flashes_from_response

    (first,) = flashes_from_response(r)
    assert "geen open positie" in first["text"]
    r = await api.post("/orders", data=form, headers=HTML)
    (again,) = flashes_from_response(r)
    assert "al verstuurd" not in again["text"] and "geen open positie" in again["text"]


# -- Review: linkify stops at quotes and angle brackets -------------------------------------------


def test_linkify_ends_the_link_at_quotes_and_brackets():
    from app.web import linkify

    html = str(linkify("<https://example.com>"))
    assert 'href="https://example.com"' in html and "&gt;</a>" not in html
    assert html.endswith("&gt;")
    html = str(linkify('https://example.com/a?b=1&c=2"><script>x</script>'))
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert html.count("<a ") == 1 and "script" not in html.split("</a>")[0]
    assert str(linkify("zie https://")) == "zie https://"


# -- Review: a test on another market is no new strategy ------------------------------------------


async def test_testing_the_active_strategy_elsewhere_offers_no_save(api):
    import respx
    from httpx import Response

    from tests.test_ux_analysis import BASE, _zigzag

    active = (await api.get("/api/strategy")).json()
    other = "ETH-EUR" if active["market"] != "ETH-EUR" else "BTC-EUR"
    with respx.mock:
        respx.get(f"{BASE}/{other}/candles").mock(return_value=Response(200, json=_zigzag()))
        body = {
            "action": "backtest",
            "strategy_json": json.dumps(active),
            "limit": "300",
            "market": other,
        }
        page = (await api.post("/strategy", data=body)).text
    assert f"Backtest op {other}" in page
    assert "Gelijk aan de actieve strategie" in page and "Nog niet opgeslagen" not in page


# -- UX-89: the brand colours come from one place --------------------------------------------------


def test_brand_tokens_are_used_and_match_the_tab_icons():
    import re
    from pathlib import Path

    root = Path(__file__).parents[1] / "app"
    css = (root / "static/app.css").read_text(encoding="utf-8")
    one = re.search(r"--brand-1:\s*(#[0-9a-f]{6})", css).group(1)
    two = re.search(r"--brand-2:\s*(#[0-9a-f]{6})", css).group(1)
    assert "var(--brand-1)" in css and "var(--brand-2)" in css
    for name in ("templates/base.html", "templates/auth/_base.html"):
        assert 'class="brand-stop-1"' in (root / name).read_text(encoding="utf-8")
    for icon in (root / "static/icons").glob("*.svg"):
        svg = icon.read_text(encoding="utf-8")
        assert one in svg and two in svg, icon.name
