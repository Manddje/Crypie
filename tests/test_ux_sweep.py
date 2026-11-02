"""The final accessibility, visual and wording sweep over every page (UX audit, sweep): shared
components, the page frame, and wording that every page shows the same way."""

import json
from types import SimpleNamespace

from sqlalchemy import delete
from starlette.datastructures import State

from app.db import Decision, Evaluation, SessionLocal
from app.strategy.model import DEFAULT_STRATEGY
from app.web import STATIC_DIR, templates
from tests.test_ux_dashboard import HTML, api  # noqa: F401 - the overview fixture (prices mocked)


def _request(path="/trading", user=None):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(user=user, flashes=[]),
        app=SimpleNamespace(state=State()),
    )


def _settings(markets=("BTC-EUR", "ETH-EUR"), mode="paper"):
    return SimpleNamespace(
        trading_mode=mode,
        markets=list(markets),
        has_bitvavo_credentials=False,
        anthropic_api_key="",
    )


def _render(body: str = "", **ctx) -> str:
    page = (
        '{% extends "base.html" %}{% import "_macros.html" as ui %}'
        "{% block title %}Crypie · Proef{% endblock %}{% block content %}" + body + "{% endblock %}"
    )
    ctx.setdefault("request", _request())
    ctx.setdefault("settings", _settings())
    ctx.setdefault("paused", False)
    return templates.env.from_string(page).render(**ctx)


def test_the_footer_names_a_few_markets_and_counts_the_rest():
    """UX-95: with 20 markets the footer stays one line."""
    few = _render()
    assert "Markten: BTC-EUR, ETH-EUR · " in few
    markets = [f"C{i:02d}-EUR" for i in range(20)]
    many = _render(settings=_settings(markets))
    assert "Markten: C00-EUR, C01-EUR, C02-EUR en 17 andere · " in many
    assert "C03-EUR" not in many
    assert '<a href="/settings#trading">aanpassen</a>' in many


def test_the_account_icon_is_marked_on_the_account_page():
    """UX-65: the account icon is the current page on /account, like a navigation link."""
    on = _render(request=_request("/account", user="kris"))
    assert 'class="icon-btn active" href="/account"' in on and 'aria-current="page"' in on
    off = _render(request=_request("/settings", user="kris"))
    assert 'class="icon-btn" href="/account"' in off


def test_the_navigation_pill_carries_the_kill_switch_text_for_app_js():
    """app.js updates the pill when the kill switch changes in another tab."""
    html = _render()
    assert 'data-paused-text="Noodstop actief: kopen gepauzeerd"' in html
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "/api/status" in app_js and "crypie:status" in app_js
    assert "fetch(" not in (STATIC_DIR / "trading.js").read_text(encoding="utf-8")


def test_shared_components_live_in_one_place():
    """One pager, one counter in segmented filters, one mode colour in running text."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for gone in (".news-pager", ".count-badge", ".trading-mode", ".strategy-mode", ".pill-blue"):
        assert gone not in css, gone
    components = css[css.index("==== 5. Onderdelen") : css.index("==== 6. Hulpklassen")]
    for shared in (".pager", ".segmented .count", ".mode-text", ".page-help", ".filter-chips"):
        assert shared in components, shared
    footer = css[css.index("==== 7. Voettekst") : css.index("==== 8. Pagina")]
    assert ".footer-alert" in footer
    # The analysis pages need no script of their own for folding and counters any more.
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "data-collapse-mobile" in app_js and "data-char-count" in app_js
    assert not (STATIC_DIR / "analysis.js").exists()


# -- One wording on every page ---------------------------------------------------------------


def test_engine_refusals_read_the_same_on_every_page():
    """UX-19/UX-105/UX-106: the engine keeps its words (tests and the API pin them); the pages
    say 'noodstop', 'orders per dag' and Dutch amounts."""
    from app.web import refusal_nl

    assert refusal_nl("handelen is gepauzeerd (kill switch)") == (
        "de noodstop staat aan: kopen is gepauzeerd"
    )
    assert (
        refusal_nl("maximum van 6 trades per dag bereikt") == "maximum van 6 orders per dag bereikt"
    )
    assert (
        refusal_nl("bedrag onder minimum van 10.0 EUR") == "bedrag onder minimum van €\u00a010,00"
    )
    assert (
        refusal_nl("blootstellingslimiet van 50.0% bereikt")
        == "blootstellingslimiet van 50% bereikt"
    )
    assert refusal_nl("dagverlieslimiet van 2.5% bereikt (gerealiseerd plus ongerealiseerd)") == (
        "dagverlieslimiet van 2,5% bereikt (gerealiseerd plus het verlies op open posities)"
    )
    assert refusal_nl(None) == ""


def test_chart_labels_use_the_journal_words_for_a_sell():
    """UX-38: one list of close reasons, also below the chart."""
    from app.labels import CLOSE_REASONS_NL
    from app.trading.history import SELL_REASONS_NL

    assert SELL_REASONS_NL["trailing_stop"] == "Verkocht: trailing stop geraakt"
    assert SELL_REASONS_NL["manual"] == "Handmatig verkocht"
    assert set(SELL_REASONS_NL) == set(CLOSE_REASONS_NL) - {"reset"}


def test_one_news_fact_is_singular():
    from app.db import Decision
    from app.trading import display

    d = Decision(
        mode="paper",
        market="BTC-EUR",
        price=1.0,
        signal="hold",
        source="rules",
        action="hold",
        reasoning="Nieuws licht negatief (-0.17, 1 nieuwsfeiten uit 1 berichten, 0 van hoge "
        "impact). Geen reden om Claude te raadplegen.",
    )
    view = display.decision_view(d, {})
    assert view.news_detail == "1 nieuwsfeit uit 1 bericht, 0 van hoge impact"


def test_dip_watch_conditions_get_readable_names():
    from app.trading import display

    labels = {"rsi14": "RSI 14", "ema200": "EMA 200"}
    text = display.readable_conditions(
        "rsi14 stijgt t.o.v. 1 candle eerder; koers hoger dan ema200", labels
    )
    assert text == "RSI 14 stijgt t.o.v. 1 candle eerder; koers hoger dan EMA 200"
    assert display.readable_conditions(None, labels) == ""


def test_live_advice_names_the_variable_last_and_links_to_the_fix():
    """UX-51: plain words first, a link, and the server variable as secondary text."""
    from app.config import Settings
    from app.main import _live_reminders

    advice = _live_reminders(Settings(), Settings())
    assert [a.env for a in advice] == ["LIVE_CAPITAL_EUR", "BITVAVO_KEY_RESTRICTED=true"]
    assert (
        advice[0].href == "/settings#f-live_capital_eur" and advice[1].href == "/settings#omgeving"
    )
    assert not any(a.text.startswith(("LIVE_", "BITVAVO_")) for a in advice)
    html = templates.env.from_string(
        '{% import "_macros.html" as ui %}{% call ui.notice("warn", "", live=false, '
        'title="Advies voor live:") %}<ul>{% for a in r %}<li>{{ a.text }} <a href="{{ a.href }}">'
        '{{ a.link }}</a> <span class="mono">{{ a.env }}</span></li>{% endfor %}</ul>{% endcall %}'
    ).render(r=advice)
    assert html.count('<div class="notice ') == 1 and 'role="' not in html
    page = (STATIC_DIR.parent / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'title="Advies voor live:"' in page and "a.env" in page


def test_one_number_rule_for_orders_and_settings():
    """UX-03: '1.000,50' is unclear in the order form as well as in the settings."""
    import pytest

    from app import settings_store as cfg
    from app.main import parse_amount_eur

    for parse in (parse_amount_eur, cfg.parse_decimal):
        with pytest.raises(ValueError, match="onduidelijk"):
            parse("1.000,50")
        with pytest.raises(ValueError, match="onduidelijk"):
            parse("2,500")
    assert parse_amount_eur("25,5") == cfg.parse_decimal("25,5") == 25.5


async def test_a_failed_test_message_says_why_in_plain_words(monkeypatch):
    import httpx

    from app import notify

    async def refuse(settings, message):
        raise httpx.ConnectError("Name or service not known")

    monkeypatch.setattr(notify, "_post", refuse)
    monkeypatch.setattr(notify, "validate_notify_url", lambda url: url)
    monkeypatch.setattr(notify, "_last_test", 0.0)
    ok, text = await notify.send_test(SimpleNamespace(notify_url="https://ntfy.example/x"))
    assert not ok
    assert text.startswith("De testmelding is niet aangekomen: het adres is niet te bereiken.")
    assert "(Technisch: ConnectError" in text


def test_market_lists_stay_short():
    from app.web import market_list

    assert market_list(["BTC-EUR", "ETH-EUR"]) == "BTC-EUR, ETH-EUR"
    assert market_list(["A", "B", "C", "D"]) == "A, B, C, D"
    assert market_list(["A", "B", "C", "D", "E"]) == "A, B, C en 2 andere"
    assert market_list([]) == "geen markten"


async def test_handelen_shows_a_refusal_in_the_app_words(api):  # noqa: F811
    with SessionLocal() as s:
        d = Decision(
            mode="paper",
            market="BTC-EUR",
            price=40000.0,
            signal="buy",
            source="rules",
            action="buy",
            blocked_by="maximum van 6 trades per dag bereikt",
        )
        s.add(d)
        s.commit()
    page = (await api.get("/trading", headers=HTML)).text
    assert "Niet uitgevoerd: maximum van 6 orders per dag bereikt" in page
    assert "trades per dag" not in page
    # The limits use the same sentences as the plan and the settings (UX-105).
    assert "(gerealiseerd plus het verlies op open posities) koopt de bot tot 00:00 UTC" in page


async def test_the_overview_points_to_a_waiting_proposal(api):  # noqa: F811
    proposal = DEFAULT_STRATEGY.model_dump(mode="json") | {
        "name": "Ruimere stop",
        "stop_loss_pct": 4,
    }
    with SessionLocal() as s:
        ev = Evaluation(
            model="claude-fable-5-1",
            period_days=7,
            report_nl="Goed.",
            proposed_strategy=json.dumps(proposal),
        )
        s.add(ev)
        s.commit()
        ev_id = ev.id
    try:
        page = (await api.get("/", headers=HTML)).text
        assert "Claude heeft een strategievoorstel gedaan: &#39;Ruimere stop&#39;" in page
        assert '<a href="/trading#voorstel">Voorstel bekijken op Handelen</a>' in page
        # The strategy card names the markets it applies to (UX-42).
        assert "Geldt voor BTC-EUR, ETH-EUR · candles van" in page
    finally:
        with SessionLocal() as s:
            s.execute(delete(Evaluation).where(Evaluation.id == ev_id))
            s.commit()


async def test_the_same_order_form_places_one_order(api):  # noqa: F811
    """UX-100: a form sent again (Back and send, a retried request) places nothing new."""
    import re

    from sqlalchemy import select

    from app.db import Order
    from app.flash import flashes_from_response
    from tests.test_ux_dashboard import _clear_trades

    _clear_trades()
    page = (await api.get("/", headers=HTML)).text
    token = re.search(r'name="order_token" value="([^"]+)"', page).group(1)
    form = {"market": "BTC-EUR", "side": "buy", "amount_quote": "20", "order_token": token}
    first = await api.post("/orders", data=form, headers=HTML)
    assert flashes_from_response(first)[0]["kind"] == "ok"
    again = await api.post("/orders", data=form, headers=HTML)
    msg = flashes_from_response(again)[-1]
    assert msg["kind"] == "warn" and "dit formulier was al verstuurd" in msg["text"]
    with SessionLocal() as s:
        assert len(s.scalars(select(Order)).all()) == 1
    # A new page has a new token; without a token (scripts, older pages) nothing changes.
    assert (
        re.search(r'name="order_token" value="([^"]+)"', (await api.get("/")).text).group(1)
        != token
    )
    _clear_trades()


async def test_handelen_points_to_trades_of_the_other_mode(api):  # noqa: F811
    """UX-01: Handelen shows the current mode; trades of the other one stay one click away."""
    from datetime import UTC, datetime

    from app.db import Position

    with SessionLocal() as s:
        p = Position(
            mode="live",
            market="BTC-EUR",
            status="closed",
            amount=0.001,
            entry_price=40000.0,
            entry_quote=40.1,
            exit_price=41000.0,
            exit_quote=40.9,
            pnl_eur=0.8,
            pnl_pct=2.0,
            close_reason="take_profit",
            closed_at=datetime.now(UTC),
        )
        s.add(p)
        s.commit()
        pid = p.id
    try:
        page = (await api.get("/trading", headers=HTML)).text
        assert "Alleen oefenmodus. Er is ook 1 trade uit live" in page
        assert '<a href="/journal?mode=live">bekijken in het journaal</a>' in page
    finally:
        with SessionLocal() as s:
            s.execute(delete(Position).where(Position.id == pid))
            s.commit()
