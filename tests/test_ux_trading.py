"""The Handelen page after the UX audit: status and controls first, one message after every
action, the kill switch's return address, a pending proposal at the top with a way back, the
cycles in pages with a summary per decision, and the evaluation report as safe markdown."""

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete

from app.db import Decision, Evaluation, Position, SessionLocal, Setting, StrategyRecord, init_db
from app.flash import flashes_from_response
from app.main import APPLIED_PROPOSALS_KEY, app
from app.strategy.model import DEFAULT_STRATEGY
from app.strategy.store import PREVIOUS_KEY, STRATEGY_KEY, load_strategy
from app.trading import display, risk
from app.trading.engine import EvaluationBusy

HTML = {"accept": "text/html,application/xhtml+xml"}
BASE = "https://api.bitvavo.com/v2"
CHECKS = {
    "buy": [
        {"text": "rsi14 lager dan 35", "left": 41.1, "right": 35.0, "ok": False, "soft": False},
        {
            "text": "rsi14 stijgt t.o.v. 1 candle eerder",
            "left": 41.1,
            "right": 40.2,
            "ok": True,
            "soft": False,
        },
        {
            "text": "koers hoger dan ema200 (zacht filter)",
            "left": 69000.0,
            "right": 70700.0,
            "ok": False,
            "soft": True,
        },
    ],
    "sell": [
        {"text": "rsi14 hoger dan 70", "left": 41.1, "right": 70.0, "ok": False, "soft": False},
    ],
}


def _clean():
    with SessionLocal() as s:
        for table in (Decision, Evaluation, Position):
            s.execute(delete(table))
        # Applying a proposal also keeps the replaced strategy (strategy:previous).
        keys = (STRATEGY_KEY, APPLIED_PROPOSALS_KEY, PREVIOUS_KEY)
        s.execute(delete(Setting).where(Setting.key.in_(keys)))
        s.execute(delete(StrategyRecord))
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


def _rules_reasoning(checks: dict) -> str:
    from app.strategy.describe import ConditionCheck, signal_reason_nl

    objs = {k: [ConditionCheck(**c) for c in v] for k, v in checks.items()}
    return (
        signal_reason_nl("hold", objs)
        + " Nieuws licht negatief (-0.17, 2 nieuwsfeiten uit 2 berichten, 1 van hoge impact). "
        "Geen reden om Claude te raadplegen."
    )


def _decision(**kw) -> Decision:
    d = Decision(
        mode="paper",
        market=kw.pop("market", "BTC-EUR"),
        price=kw.pop("price", 100.0),
        signal=kw.pop("signal", "hold"),
        checks=json.dumps(CHECKS),
        news_score=-0.17,
        news_label="licht negatief",
        source=kw.pop("source", "rules"),
        action=kw.pop("action", "hold"),
        reasoning=kw.pop("reasoning", _rules_reasoning(CHECKS)),
        **kw,
    )
    return d


def _add_decisions(*decisions: Decision, spread_min: int = 30) -> None:
    now = datetime.now(UTC)
    with SessionLocal() as s:
        for i, d in enumerate(decisions):
            d.created_at = now - timedelta(minutes=spread_min * (len(decisions) - i))
            s.add(d)
        s.commit()


def _add_evaluation(proposal: dict | None = None, report: str = "## Resultaat\nGoed.") -> int:
    with SessionLocal() as s:
        ev = Evaluation(
            model="claude-fable-5-1",
            period_days=7,
            report_nl=report,
            proposed_strategy=json.dumps(proposal) if proposal is not None else None,
        )
        s.add(ev)
        s.commit()
        return ev.id


def _proposal(**overrides) -> dict:
    return DEFAULT_STRATEGY.model_dump(mode="json") | overrides


def _rows(page: str) -> int:
    return page.count('<tbody class="cycle-entry')


# -- the page as a whole --------------------------------------------------------------------


async def test_page_order_status_first_and_no_inline_styles(api):
    page = (await api.get("/trading")).text
    order = [
        'class="hero trading-hero"',
        'id="laatste-cyclus"',
        'id="open-posities"',
        'id="context"',
        'id="gesloten-posities"',
        'id="evaluaties"',
        'id="cycli"',  # the long log last
    ]
    positions = [page.index(x) for x in order]
    assert positions == sorted(positions)
    main = page.split('id="main"', 1)[1]
    assert "style=" not in main and "<script>" not in page
    assert '<script src="/static/trading.js?v=' in page
    # The headings do not skip a level: h1, then h2 per card, h3 inside.
    levels = [int(n) for n in re.findall(r"<h([1-6])", main)]
    assert levels[0] == 1 and all(b - a <= 1 for a, b in zip(levels, levels[1:], strict=False))
    # One status from bot_health() in the hero, with the cycle times and the exit watch.
    assert "status status-" in page and "Status van de bot" in page
    assert "Laatste cyclus" in page and "Bewaking stop-loss/take-profit:" in page
    assert "(elke 2 min)" in page
    # Without a Claude key it is said once (in the status), not in a separate notice.
    assert page.count("geen Claude-sleutel") == 1
    assert "Claude: niet ingesteld" not in page and "Winstkans" not in page


async def test_subtitle_names_the_mode_and_the_cycle_asks_in_live(api, monkeypatch):
    page = (await api.get("/trading")).text
    assert "in oefenmodus, met fictief geld" in page
    assert "data-confirm-if-live=" in page and 'data-busy="Cyclus loopt…"' in page
    _add_evaluation(_proposal(name="Strakker", stop_loss_pct=3))
    monkeypatch.setattr(app.state.engine.broker, "mode", "live")
    page = (await api.get("/trading")).text
    assert "live, met echt geld op Bitvavo" in page and "mode-text is-live" in page
    # Applying a proposal in live says so in its confirmation.
    assert "data-confirm=\"LIVE · echt geld: Strategie 'Strakker' activeren" in page


async def test_kill_switch_stays_with_one_sentence_when_trading_is_off(api, monkeypatch):
    off = app.state.settings.model_copy(update={"trading_enabled": False})
    monkeypatch.setattr(app.state, "settings", off)
    page = (await api.get("/trading")).text
    assert "status-off" in page and "Uitgeschakeld" in page
    assert "Automatisch handelen staat uit, dus er draaien geen geplande cycli." in page
    assert 'value="pause"' in page and "alleen als je er zelf een start" in page


async def test_exit_watch_is_red_when_it_falls_behind(api, monkeypatch):
    eng = app.state.engine
    monkeypatch.setattr(eng, "exit_failing_since", datetime.now(UTC) - timedelta(minutes=9))
    monkeypatch.setattr(eng, "exit_error", "koersen niet op te halen")
    page = (await api.get("/trading")).text
    assert 'class="is-bad">Bewaking stop-loss/take-profit:' in page
    assert "loopt achter" in page and "status-error" in page


async def test_limit_meters_show_the_current_figures(api):
    page = (await api.get("/trading")).text
    assert "In crypto" in page and "van max. 50%" in page
    assert "Resultaat vandaag" in page and "grens -5%" in page
    assert "Orders vandaag" in page and "Maximum aantal orders per dag" in page
    assert page.count('<progress class="meter') == 3
    assert 'aria-labelledby="meter-orders"' in page
    # Agenda, trend and the full list of limits are folded away, open on risk-off.
    assert '<details class="context-more">' in page and "Harde limieten" in page


async def test_open_positions_are_the_full_version(api, monkeypatch):
    with SessionLocal() as s:
        s.add(
            Position(
                mode="paper",
                market="BTC-EUR",
                amount=0.001,
                entry_price=60000.0,
                entry_quote=60.15,
                stop_price=57000.0,
            )
        )
        s.commit()
    monkeypatch.setattr(app.state.engine, "last_prices", {"BTC-EUR": 61000.0})
    monkeypatch.setattr(app.state.engine, "last_prices_at", datetime.now(UTC))
    page = (await api.get("/trading")).text
    card = page.split('id="open-posities"', 1)[1].split("</section>", 1)[0]
    assert "Resultaat (netto)" in card and "koersen van" in card
    # 0.001 * 61000 = 61,00 minus 0,25% sell fee = 60,8475; minus 60,15 spent = +0,70.
    assert "+€\xa00,70" in card and "€\xa061.000,00" in card
    assert "6,6% onder de koers" in card  # (61000 - 57000) / 61000


# -- actions: one message each, and back where you came from -----------------------------------


async def test_kill_switch_flashes_and_returns_only_to_known_pages(api):
    r = await api.post("/trading/action", data={"action": "pause", "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert flashes_from_response(r) == [{"kind": "ok", "text": display.PAUSED_FLASH}]
    with SessionLocal() as s:
        assert risk.is_paused(s)
    for target, where in (("/trading", "/trading"), ("", "/trading"), ("/journal", "/")):
        r = await api.post("/trading/action", data={"action": "resume", "next": target})
        assert r.headers["location"] == where, target
    for evil in ("//evil.example", "https://evil.example/", "/trading?x=1", "/\\evil"):
        r = await api.post("/trading/action", data={"action": "pause", "next": evil})
        assert r.headers["location"] == "/", evil
    r = await api.post("/trading/action", data={"action": "resume"})
    # (The client keeps the flash cookie: no page was shown in between, so the newest is last.)
    assert flashes_from_response(r)[-1] == {"kind": "ok", "text": display.RESUMED_FLASH}
    # The page shows the paused state with the one kill-switch sentence and a resume button.
    await api.post("/trading/action", data={"action": "pause"})
    page = (await api.get("/trading")).text
    assert "Noodstop actief" in page and 'value="resume"' in page and "password" not in page


async def test_unknown_action_is_refused(api):
    assert (await api.post("/trading/action", data={"action": "bogus"})).status_code == 400
    r = await api.post("/trading/action", data={"action": "bogus"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/trading"
    assert flashes_from_response(r)[0]["kind"] == "error"


@respx.mock
async def test_cycle_result_comes_back_as_a_message(api):
    respx.get(f"{BASE}/ticker/price").mock(
        return_value=Response(
            200, json=[{"market": "BTC-EUR", "price": "100"}, {"market": "ETH-EUR", "price": "10"}]
        )
    )
    flat = [[1_700_000_000_000 + i * 3_600_000, "100", "101", "99", "100", "1"] for i in range(60)]
    respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=flat))
    respx.get(f"{BASE}/ETH-EUR/candles").mock(return_value=Response(200, json=flat))
    r = await api.post("/trading/action", data={"action": "cycle", "next": "/"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert flashes_from_response(r) == [
        {"kind": "ok", "text": "Cyclus gedraaid: BTC-EUR afwachten · ETH-EUR afwachten."}
    ]


async def test_cycle_failure_returns_to_the_page_it_came_from(api, monkeypatch):
    from app.bitvavo.client import BitvavoNetworkError

    async def down(markets):
        raise BitvavoNetworkError(TimeoutError("read timed out"))

    monkeypatch.setattr(app.state.engine.client, "ticker_prices", down)
    r = await api.post("/trading/action", data={"action": "cycle", "next": "/"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/"
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "error" and msg["text"].startswith("Cyclus niet gedraaid: Bitvavo")


async def test_evaluate_without_key_explains_why(api):
    page = (await api.get("/trading")).text
    assert 'disabled aria-describedby="evaluate-why"' in page
    assert 'id="evaluate-why">Evalueren kan pas met een Claude-sleutel' in page
    assert "Automatische evaluaties staan uit zolang de Claude-sleutel ontbreekt" in page
    assert "of klik op" not in page
    r = await api.post("/trading/action", data={"action": "evaluate"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/trading"
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "error" and "Claude-sleutel ontbreekt" in msg["text"]
    assert (await api.post("/trading/action", data={"action": "evaluate"})).status_code == 409


async def test_evaluate_with_key_reports_the_outcome(api, monkeypatch):
    eng = app.state.engine
    monkeypatch.setattr(eng, "evaluator", SimpleNamespace(available=True, model="x"))

    async def run(session, days=7):
        ev_id = _add_evaluation(_proposal(name="Strakker", stop_loss_pct=3))
        return session.get(Evaluation, ev_id)

    monkeypatch.setattr(eng, "run_evaluation", run)
    page = (await api.get("/trading")).text
    desc = page.split('id="evaluaties"', 1)[1].split("</p>", 1)[0]
    assert "kijkt Claude " in desc and "claude-" not in desc  # a readable model name
    r = await api.post("/trading/action", data={"action": "evaluate"}, headers=HTML)
    (msg,) = flashes_from_response(r)
    assert msg["kind"] == "ok" and "Claude stelt de strategie 'Strakker' voor" in msg["text"]

    async def busy(session, days=7):
        raise EvaluationBusy("Er loopt al een evaluatie; probeer het straks opnieuw.")

    monkeypatch.setattr(eng, "run_evaluation", busy)
    r = await api.post("/trading/action", data={"action": "evaluate"}, headers=HTML)
    msg = flashes_from_response(r)[-1]
    assert msg["kind"] == "error" and msg["text"].startswith("Er loopt al een evaluatie")


# -- proposals: at the top, confirmed, and with a way back -------------------------------------


async def test_pending_proposal_is_at_the_top_and_can_be_put_back(api):
    stale_id = _add_evaluation(_proposal(name="Ouder voorstel", stop_loss_pct=4))
    ev_id = _add_evaluation(_proposal(name="Strakker", stop_loss_pct=3))
    page = (await api.get("/trading")).text
    assert page.index('id="voorstel"') < page.index('id="laatste-cyclus"')
    assert "Voorstel van Claude: Strakker" in page
    assert page.count(f'value="apply:{ev_id}"') == 1  # at the top, not again in the list
    assert f'value="apply:{stale_id}"' in page  # an older proposal stays in its evaluation
    assert "Strategie 'Strakker' activeren in plaats van" in page  # the confirm question
    assert "Het voorstel 'Strakker' staat bovenaan deze pagina" in page

    r = await api.post("/trading/action", data={"action": f"apply:{ev_id}"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/trading"
    assert flashes_from_response(r) == [
        {
            "kind": "ok",
            "text": "Strategie 'Strakker' is nu actief. Open posities houden hun huidige "
            "stop-loss en take-profit.",
        }
    ]
    page = (await api.get("/trading")).text
    # The change stays visible (before and after) with the way back, at the top for now.
    assert "Net toegepast: Strakker" in page and "Wat er veranderde op" in page
    assert '<th scope="col">Vóór</th>' in page and "Stop-loss (%)" in page
    assert page.count(f'value="revert:{ev_id}"') == 1 and "voorstel toegepast" in page

    r = await api.post("/trading/action", data={"action": f"revert:{ev_id}"}, headers=HTML)
    (msg,) = flashes_from_response(r)
    assert msg["text"].startswith(f"De vorige strategie '{DEFAULT_STRATEGY.name}' is weer actief.")
    with SessionLocal() as s:
        assert load_strategy(s).name == DEFAULT_STRATEGY.name
    page = (await api.get("/trading")).text
    assert f'value="revert:{ev_id}"' not in page and "teruggezet naar" in page
    # Only once, and not by scripts either.
    r = await api.post("/trading/action", data={"action": f"revert:{ev_id}"})
    assert r.status_code == 409 and "al teruggezet" in r.json()["detail"]


async def test_revert_never_undoes_a_later_change(api):
    ev_id = _add_evaluation(_proposal(name="Strakker", stop_loss_pct=3))
    assert (await api.post(f"/api/evaluations/{ev_id}/apply")).status_code == 200
    changed = _proposal(name="Zelf aangepast", stop_loss_pct=2)
    assert (await api.put("/api/strategy", json=changed)).status_code == 200
    page = (await api.get("/trading")).text
    assert f'value="revert:{ev_id}"' not in page and "terugzetten kan hier niet meer" in page
    r = await api.post("/trading/action", data={"action": f"revert:{ev_id}"})
    assert r.status_code == 409 and "nog gewijzigd" in r.json()["detail"]
    with SessionLocal() as s:
        assert load_strategy(s).name == "Zelf aangepast"
    for bad in ("revert:", "revert:x", "revert:" + "9" * 12):
        assert (await api.post("/trading/action", data={"action": bad})).status_code == 400
    assert (await api.post("/trading/action", data={"action": "revert:999999"})).status_code == 409


async def test_proposal_that_cannot_be_applied_is_not_at_the_top(api):
    _add_evaluation(_proposal(name="Zonder stop", stop_loss_pct=None, trailing_stop_pct=None))
    page = (await api.get("/trading")).text
    assert 'id="voorstel"' not in page and "Dit voorstel kan niet worden toegepast" in page


async def test_report_is_safe_markdown_with_a_readable_model_name(api):
    _add_evaluation(
        report="## Resultaat\n- **Twee** trades\n<script>alert(1)</script> [klik](https://evil.example)"
    )
    page = (await api.get("/trading")).text
    card = page.split('id="evaluaties"', 1)[1]
    assert "## Resultaat" not in card and "<h4>Resultaat</h4>" in card
    assert "<strong>Twee</strong>" in card
    assert "<script>alert" not in card and "&lt;script&gt;" in card
    assert "evil.example" not in card and "klik" in card
    assert "Claude Fable 5.1" in card and "claude-fable-5-1" not in card


# -- Alle cycli: important first, in pages, a summary per decision -----------------------------


async def test_cycles_default_to_the_important_ones_in_pages(api):
    quiet = [_decision(market="ETH-EUR" if i % 2 else "BTC-EUR") for i in range(60)]
    claude = _decision(
        source="claude",
        action="buy",
        signal="buy",
        confidence=0.7,
        amount_quote=25.0,
        executed=True,
        reasoning="De RSI draait omhoog; ik koop klein.",
    )
    blocked = _decision(signal="buy", blocked_by="risk-off", reasoning="Risk-off: FOMC.")
    stop = _decision(
        source="mechanical",
        action="sell",
        executed=True,
        has_position=True,
        reasoning="stop_loss geraakt op 66091.50",
    )
    _add_decisions(*quiet[:30], claude, blocked, *quiet[30:], stop)
    page = (await api.get("/trading")).text
    assert _rows(page) == 3
    assert 'Belangrijk <span class="count">3</span>' in page
    assert 'Alles <span class="count">63</span>' in page
    assert "Stop-loss geraakt op €\xa066.091,50" in page
    assert "Geblokkeerd" in page and "De RSI draait omhoog; ik koop klein." in page

    page = (await api.get("/trading?view=all")).text
    assert _rows(page) == 25 and "1–25 van 63 · pagina 1 van 3" in page
    assert 'href="/trading?view=all&amp;page=2#cycli" rel="next"' in page
    page = (await api.get("/trading?view=all&page=3")).text
    assert _rows(page) == 13 and 'rel="prev"' in page and 'rel="next"' not in page
    assert _rows((await api.get("/trading?view=all&page=99")).text) == 13  # last page
    page = (await api.get("/trading?view=all&market=ETH-EUR")).text
    assert _rows(page) == 25 and "1–25 van 30" in page
    assert 'class="active" aria-current="true">ETH-EUR</a>' in page
    page = (await api.get("/trading?market=XRP-EUR")).text
    assert "Geen beslissingen in deze selectie" in page and "Filters wissen" in page
    assert (await api.get("/trading?view=alles")).status_code == 422
    r = await api.get("/trading?view=alles", headers=HTML)
    assert r.status_code == 400 and 'href="/trading"' in r.text


async def test_decision_summary_first_without_repeating_the_checklist(api):
    _add_decisions(_decision(market="BTC-EUR"), _decision(market="ETH-EUR"))
    page = (await api.get("/trading?view=all")).text
    assert "1 van 2 koopvoorwaarden gehaald · 1 zacht filter niet gehaald" in page
    # The engine's sentence that repeats every condition is not shown again under them.
    assert "Geen koopsignaal, 1 van 2 harde koopvoorwaarden" not in page
    assert "Geen reden om Claude te raadplegen." in page
    # The news counts move to the news line, with a Dutch score.
    assert "score -0,17" in page and "2 nieuwsfeiten uit 2 berichten" in page
    assert "(-0.17," not in page
    # Readable names, and the checklist folds away per row in the log.
    assert "RSI 14 lager dan 35" in page and "rsi14 lager dan 35" not in page
    assert '<details class="cycle-checks">' in page
    # One visible key for the marks, at the first checklist.
    assert page.count('class="checks-legend"') == 1


async def test_only_quiet_decisions_offer_the_full_view(api):
    _add_decisions(_decision())
    page = (await api.get("/trading")).text
    assert "Geen belangrijke beslissingen" in page and "Alles tonen" in page


# -- display helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,name",
    [
        ("claude-fable-5-1", "Claude Fable 5.1"),
        ("claude-opus-4-1-20250805", "Claude Opus 4.1"),
        ("claude-3-5-haiku-latest", "Claude 3.5 Haiku"),
        ("", "Claude"),
    ],
)
def test_model_name(model, name):
    assert display.model_name_nl(model) == name


def test_readable_condition():
    labels = display.indicator_labels(DEFAULT_STRATEGY)
    assert display.readable_condition("rsi14 lager dan 35", labels) == "RSI 14 lager dan 35"
    assert (
        display.readable_condition("koers hoger dan ema200 (zacht filter)", labels)
        == "koers hoger dan EMA 200 (zacht filter)"
    )
    assert display.readable_condition("onbekend iets", labels) == "onbekend iets"


def test_cycle_flash_warns_when_an_order_did_not_go_through():
    ok = SimpleNamespace(
        market="BTC-EUR", signal="buy", action="buy", source="claude", executed=True
    )
    refused = SimpleNamespace(
        market="ETH-EUR", signal="buy", action="buy", source="claude", executed=False
    )
    assert display.cycle_flash([ok]) == ("ok", "Cyclus gedraaid: BTC-EUR gekocht.")
    kind, text = display.cycle_flash([ok, refused])
    assert kind == "warn" and "ETH-EUR kopen niet uitgevoerd" in text
    assert display.cycle_flash([])[0] == "warn"


def test_position_result_is_net_of_both_fees():
    p = Position(amount=2.0, entry_price=10.0, entry_quote=20.05, stop_price=9.0)
    v = display.position_view(p, 11.0, 0.25)
    assert v["value_eur"] == 22.0
    assert round(v["pnl_eur"], 4) == round(22.0 * 0.9975 - 20.05, 4)
    assert round(v["stop_gap_pct"], 2) == round(2 / 11 * 100, 2)
    assert display.position_view(p, None, 0.25)["pnl_eur"] is None


async def test_blocked_buys_are_judged_in_hindsight(api):
    """Under Agenda en risk-off: per blockade the price then and the move after 6 and 24 hours,
    from the journal's own prices; a blocked sell does not count as a blocked buy."""
    blocked = [
        _decision(signal="buy", price=69000 - 20 * i, blocked_by="risk-off", reasoning="Risk-off.")
        for i in range(12)
    ]
    later = [_decision(price=68310) for _ in range(20)]
    sell = _decision(signal="sell", has_position=True, blocked_by="order onderweg")
    _add_decisions(*blocked, *later, sell, spread_min=30)
    page = (await api.get("/trading")).text
    assert 'id="blocked-review"' in page
    assert "Risk-off" in page and "Geblokkeerde aankopen achteraf" in page
    assert "12× in 5,5 uur" in page and "€\xa069.000,00" in page
    assert "-1,0%" in page  # 6 h after the first blocked signal
    assert "Na 24 uur" not in page and "Na 6 uur stond de koers in 1 van de 1 geval lager" in page


async def test_blocked_review_is_quiet_without_blockades(api):
    page = (await api.get("/trading")).text
    assert "Geen tegengehouden aankopen." in page and "laatste 30 dagen" in page
