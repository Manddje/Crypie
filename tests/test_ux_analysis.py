"""The analysis pages after the UX audit: Journaal, Nieuws, Tradingplan and Strategie.

Journal: the default filter form works ('alles' is days=0, an empty field is no filter), close
reasons are Dutch and filterable, manual trades are not Claude's, active filters are summed up
with links that remove them, and the other mode can be shown. News: no silent cap (paging and
counts over the whole period), no red error after a refresh without a key, readable details and
English titles marked as English. Plan and strategy: see the sections below.
"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete

from app.db import (
    Decision,
    Evaluation,
    NewsItem,
    Order,
    Position,
    SessionLocal,
    Setting,
    init_db,
)
from app.flash import flashes_from_response
from app.main import app
from app.trading import risk
from app.trading.journal import JournalFilter, journal_entries, journal_totals, order_source

HTML = {"accept": "text/html,application/xhtml+xml"}


def _clean():
    with SessionLocal() as s:
        for table in (Decision, Order, Position, NewsItem, Evaluation):
            s.execute(delete(table))
        s.execute(
            delete(Setting).where(
                Setting.key.in_(("strategy", "strategy:previous", "trading_plan"))
            )
        )
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


# -- Journal ------------------------------------------------------------------------------


def _trade(
    s,
    market: str,
    pnl: float,
    close_reason: str | None,
    *,
    buy_reason: str = "claude: dip",
    source: str | None = "claude",
    why: str = "RSI draait omhoog",
    mode: str = "paper",
    hours_ago: float = 5,
) -> Position:
    """A round trip with its orders (and a decision unless the buy was manual)."""
    opened = datetime.now(UTC) - timedelta(hours=hours_ago)
    buy = Order(
        mode=mode,
        market=market,
        side="buy",
        order_type="market",
        status="filled",
        reason=buy_reason,
    )
    buy.amount, buy.amount_quote, buy.price, buy.fee_quote = 0.001, 50.0, 50000.0, 0.13
    s.add(buy)
    s.flush()
    p = Position(
        mode=mode,
        market=market,
        status="closed" if close_reason else "open",
        amount=0.001,
        entry_price=50000.0,
        entry_quote=50.0,
        opened_at=opened,
        open_order_id=buy.id,
    )
    if close_reason:
        sell = Order(
            mode=mode,
            market=market,
            side="sell",
            order_type="market",
            status="filled",
            reason=close_reason,
        )
        sell.amount, sell.amount_quote, sell.price = 0.001, 50.0 + pnl, 50000.0
        s.add(sell)
        s.flush()
        p.close_order_id = sell.id
        p.exit_price, p.exit_quote = 50000.0, 50.0 + pnl
        p.pnl_eur, p.pnl_pct = pnl, pnl / 50 * 100
        p.closed_at, p.close_reason, p.fees_eur = opened + timedelta(hours=1), close_reason, 0.25
    s.add(p)
    s.flush()
    if source:
        s.add(
            Decision(
                mode=mode,
                market=market,
                price=50000.0,
                action="buy",
                signal="buy",
                source=source,
                reasoning=why,
                order_id=buy.id,
                executed=True,
            )
        )
    s.commit()
    return p


def test_order_source_splits_the_prefix():
    assert order_source("manual: handmatige test") == ("manual", "handmatige test")
    assert order_source("manual") == ("manual", "")
    assert order_source("claude: dip gekocht") == ("claude", "dip gekocht")
    assert order_source("stop_loss") == (None, "stop_loss")


async def test_journal_default_form_with_all_periods_works(api):
    """UX-36: 'alles' is days=0 and an empty field is no filter, not a JSON 422."""
    r = await api.get("/journal?market=&status=&result=&reason=&by=&days=0&tag=&q=", headers=HTML)
    assert r.status_code == 200 and "Ongeldig adres" not in r.text
    assert '<option value="0">alles</option>' in r.text
    assert (await api.get("/journal?days=", headers=HTML)).status_code == 200
    assert (await api.get("/api/journal?days=")).status_code == 200
    # Out of range still refused.
    assert (await api.get("/journal?days=999999999")).status_code == 422


async def test_manual_trades_are_not_claudes(api):
    """UX-39: the label follows the source; a manual trade gets a pill and 'Jouw reden'."""
    with SessionLocal() as s:
        manual = _trade(
            s, "ETH-EUR", -0.2, "manual", buy_reason="manual: handmatige test", source=None
        )
        bot = _trade(s, "BTC-EUR", 2.0, "signal", why="Draai bevestigd")
        rules = _trade(s, "BTC-EUR", 1.0, "take_profit", source="rules", why="Signaal gevolgd")
        mid, bid, rid = manual.id, bot.id, rules.id
    r = await api.get("/journal")
    page = r.text
    manual_block = page[page.index(f'id="trade-{mid}"') :]
    manual_block = manual_block[: manual_block.index("</article>")]
    assert "Handmatig</span>" in manual_block and "Jij, handmatig" in manual_block
    assert "Jouw reden:</span> handmatige test" in manual_block
    assert "Claude destijds" not in manual_block and "manual:" not in manual_block
    assert "Handmatig verkocht" in manual_block  # Dutch close reason (UX-38)
    bot_block = page[page.index(f'id="trade-{bid}"') :]
    assert "Claude destijds:</span> Draai bevestigd" in bot_block[: bot_block.index("</article>")]
    rules_block = page[page.index(f'id="trade-{rid}"') :]
    rules_block = rules_block[: rules_block.index("</article>")]
    assert "Regels (zonder Claude)" in rules_block and "Claude destijds" not in rules_block
    assert "Uitleg van de regels:" in rules_block
    # The market is a heading that names the trade (UX-77).
    assert f'<h3 class="trade-title" id="trade-{mid}-title">ETH-EUR' in page
    assert f'aria-labelledby="trade-{mid}-title"' in page

    # Filter on who opened it, with separate totals.
    r = await api.get("/journal?by=manual")
    assert f'id="trade-{mid}"' in r.text and f'id="trade-{bid}"' not in r.text
    r = await api.get("/journal?by=bot")
    assert f'id="trade-{bid}"' in r.text and f'id="trade-{mid}"' not in r.text
    body = (await api.get("/api/journal")).json()
    assert {e["opened_by"] for e in body["entries"]} == {"manual", "claude", "rules"}
    assert [b["by"] for b in body["totals"]["by_source"]] == ["bot", "manual"]


async def test_close_reason_filter_and_unknown_values(api):
    """UX-38: 'manual' and 'trailing_stop' are filterable; an unknown value is reported."""
    with SessionLocal() as s:
        _trade(s, "BTC-EUR", -1.0, "trailing_stop")
        _trade(s, "ETH-EUR", -0.2, "manual", buy_reason="manual", source=None)
    r = await api.get("/journal?reason=manual")
    assert "1 in de selectie" in r.text and "Hoe gesloten: handmatig verkocht" in r.text
    r = await api.get("/journal?reason=trailing_stop")
    assert "1 in de selectie" in r.text and "Trailing stop geraakt" in r.text
    assert '<option value="trailing_stop" selected>' in r.text
    r = await api.get("/journal?reason=xyz")
    assert "Niet begrepen en genegeerd: hoe gesloten &#39;xyz&#39;" in r.text
    assert "2 in totaal" in r.text
    # No English label left in the breakdown or the trade header.
    r = await api.get("/journal")
    assert "Trailing_stop" not in r.text and ">Manual<" not in r.text


async def test_active_filters_are_summed_up_with_remove_links(api):
    """UX-40: each active filter is a label whose link removes only that filter."""
    with SessionLocal() as s:
        _trade(s, "ETH-EUR", 1.0, "signal")
    r = await api.get("/journal?market=ETH-EUR&days=30&result=win")
    chips = dict(re.findall(r'class="tag chip-remove" href="([^"]+)">([^<]+)<', r.text))
    assert chips == {
        "/journal?result=win&amp;days=30": "Markt: ETH-EUR",
        "/journal?market=ETH-EUR&amp;days=30": "Resultaat: winst",
        "/journal?market=ETH-EUR&amp;result=win": "Periode: laatste 30 dagen",
    }
    assert "Resultaat van de selectie" in r.text and "Alle filters wissen" in r.text
    assert '<span class="pill">3 actief</span>' in r.text
    # The filter block folds away on a phone (app.js) and the note form returns here.
    assert "data-collapse-mobile" in r.text and "/static/app.js" in r.text
    assert 'name="back" value="/journal?market=ETH-EUR&amp;result=win&amp;days=30"' in r.text


async def test_journal_shows_the_other_mode_on_request(api):
    """UX-01: trades of the other mode are not mixed in, but can be shown on purpose."""
    with SessionLocal() as s:
        _trade(s, "BTC-EUR", 3.0, "signal", mode="live", why="Live trade")
        _trade(s, "ETH-EUR", 1.0, "signal", why="Oefentrade")
    r = await api.get("/journal")
    assert "Oefentrade" in r.text and "Live trade" not in r.text
    assert 'aria-label="Trades uit welke modus"' in r.text and 'aria-current="true"' in r.text
    r = await api.get("/journal?mode=live")
    assert "Live trade" in r.text and "Oefentrade" not in r.text
    assert "Je bekijkt de trades uit live" in r.text
    assert (await api.get("/api/journal?mode=live")).json()["mode"] == "live"


async def test_journal_note_links_and_saved_message_in_the_trade(api):
    """UX-98 and UX-99: a long address in a note is a safe link; 'opgeslagen' in the trade."""
    with SessionLocal() as s:
        pid = _trade(s, "BTC-EUR", 1.0, "signal").id
    url = "https://example.com/" + "a" * 150
    r = await api.post(
        f"/journal/{pid}/note", data={"note": f"Bron {url} <b>x</b>", "back": "/journal"}
    )
    assert r.status_code == 303
    r = await api.get(f"/journal?saved={pid}")
    block = r.text[r.text.index(f'id="trade-{pid}"') :]
    block = block[: block.index("</article>")]
    assert "Notitie opgeslagen." in block
    assert f'<a href="{url}" target="_blank" rel="noopener noreferrer">' in block
    assert "<b>x</b>" not in block and "&lt;b&gt;x&lt;/b&gt;" in block


def test_journal_totals_count_winning_trades_and_sources():
    """UX-106: totals per source; results coloured on the rounded value (UX-31)."""
    with SessionLocal() as s:
        _clean()
        _trade(s, "BTC-EUR", 2.0, "signal")
        _trade(s, "BTC-EUR", -1.0, "manual", buy_reason="manual", source=None)
        entries = journal_entries(s, "paper", JournalFilter())
        totals = journal_totals(entries)
        _clean()
    assert totals["wins"] == 1 and totals["closed"] == 2
    assert {b["by"]: b["trades"] for b in totals["by_source"]} == {"bot": 1, "manual": 1}


async def test_journal_page_colours_and_words(api):
    with SessionLocal() as s:
        _trade(s, "BTC-EUR", 0.001, "signal")  # rounds to € 0,00: not green
    r = await api.get("/journal")
    assert "Winnende trades" in r.text and "Winstkans" not in r.text
    assert "1 afgeronde trade<" in r.text or "1 afgeronde trade " in r.text
    assert 'class="value pos"' not in r.text


# -- News ---------------------------------------------------------------------------------


def _news(s, n: int, *, assessed: bool = False, hours_ago: float = 1.0, **kw) -> None:
    now = datetime.now(UTC)
    for i in range(n):
        s.add(
            NewsItem(
                source="rss",
                source_name=f"Bron {i}",
                feed_host=f"bron{i}.test",
                url=f"https://bron{i}.test/{kw.get('prefix', 'a')}{i}",
                title=kw.get("title", "Bitcoin rallies after the ETF approval {i}").format(i=i),
                published_at=now - timedelta(hours=hours_ago, minutes=i),
                assessed=assessed,
                relevant=kw.get("relevant"),
                sentiment=kw.get("sentiment"),
                impact=kw.get("impact"),
                horizon=kw.get("horizon"),
                coins=kw.get("coins"),
                event_key=kw.get("event_key", f"feit-{kw.get('prefix', 'a')}-{i}") or None,
            )
        )
    s.commit()


async def test_news_list_has_no_silent_cap(api):
    """UX-37: every fact of the period is counted; the list is paged, 30 per page."""
    with SessionLocal() as s:
        _news(s, 200)
        # One fact that counts, published before the 200 others (the old cap hid it).
        _news(
            s,
            1,
            assessed=True,
            hours_ago=10,
            prefix="hack",
            title="Exchange hack drains $40m in Ether",
            relevant=True,
            sentiment=-0.8,
            impact=2,
            horizon="hours",
            coins="ETH",
            event_key="eth-hack",
        )
    r = await api.get("/news")
    assert "201 nieuwsfeiten uit 201 berichten" in r.text
    assert "1–30 van 201 · pagina 1 van 7" in r.text
    assert re.search(r'Telt mee<span class="count">1</span>', r.text)
    assert re.search(r'Telt niet mee<span class="count">200</span>', r.text)
    assert 'rel="next"' in r.text and r.text.count('class="news-item') == 30
    r = await api.get("/news?telt=ja")
    assert "Exchange hack drains" in r.text and "1–1 van 1" in r.text
    r = await api.get("/news?page=7")
    assert "181–201 van 201 · pagina 7 van 7" in r.text and "Exchange hack drains" in r.text
    # A page beyond the last shows the last.
    assert "pagina 7 van 7" in (await api.get("/news?page=99")).text
    # The coin card links its top fact to that fact in the list of facts that count.
    m = re.search(r'href="(/news\?hours=48&amp;telt=ja#feit-[0-9a-f]{10})"', r.text)
    assert m is not None
    anchor = m.group(1).split("#")[1]
    assert f'id="{anchor}"' in (await api.get("/news?hours=48&telt=ja")).text


async def test_news_marks_english_titles_and_reads_well(api):
    """UX-79 and UX-45: lang="en", Dutch details, external links announced."""
    with SessionLocal() as s:
        _news(s, 1, title="Bitcoin rallies after the ETF approval")
        _news(s, 1, prefix="nl", title="Bitcoin stijgt na de goedkeuring van een ETF")
        _news(
            s,
            1,
            assessed=True,
            prefix="eth",
            title="Ether jumps on upgrade",
            relevant=True,
            sentiment=0.6,
            impact=2,
            horizon="days",
            coins="ETH",
        )
    r = await api.get("/news")
    assert 'lang="en">Bitcoin rallies after the ETF approval' in r.text
    assert '>Bitcoin stijgt na de goedkeuring van een ETF<span class="visually-hidden">' in r.text
    assert "(opent in nieuw tabblad)" in r.text
    assert "impact middel · effect: dagen" in r.text and "positief +0,60" in r.text
    # 'telt mee' is not repeated in the reason next to it.
    assert 'telt mee</span> <span class="count-reason">voor ETH</span>' in r.text
    assert "niet beoordeeld (geen Claude-sleutel)" in r.text
    assert "Nieuwsbeoordeling: uit (geen Claude-sleutel)" in r.text
    # Headings: h2 sections, h3 per coin and per fact (UX-77); filters say which one is on.
    assert '<h2 id="sentiment-title">' in r.text and '<h3 class="title"' in r.text
    assert 'role="group" aria-label="Telt mee in het sentiment"' in r.text
    assert 'class="active" aria-current="true">Alles' in r.text
    # The long explanation is folded away (UX-107); no inline style left on the page.
    assert "<summary>Hoe telt nieuws mee?</summary>" in r.text
    assert 'style="' not in r.text.split("<main", 1)[1]


async def test_news_empty_states_match_the_data(api):
    """UX-78: no 'Alles telt mee' when there is no news at all."""
    r = await api.get("/news?telt=nee")
    assert "Nog geen berichten" in r.text and "Alles telt mee" not in r.text


async def test_news_refresh_without_key_is_not_an_error(api, monkeypatch):
    """UX-41 and UX-99: one message after the refresh, back to the same view."""
    from app.news.service import NewsService

    async def fake_refresh(self, session, max_batches=4):
        self.last_error = None
        self.last_run = datetime.now(UTC)
        return {
            "fetched": 9,
            "inserted": 3,
            "assessed": 0,
            "assessing": False,
            "pending": 3,
            "error": None,
        }

    monkeypatch.setattr(NewsService, "refresh", fake_refresh)
    r = await api.post("/news/refresh", data={"hours": "24", "telt": "ja"}, headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/news?hours=24&telt=ja#berichten"
    assert flashes_from_response(r) == [
        {
            "kind": "info",
            "text": "Nieuws ververst: 3 nieuwe berichten (niet beoordeeld: geen Claude-sleutel).",
        }
    ]
    r = await api.get("/news?hours=24&telt=ja")
    assert "notice-error" not in r.text and "ging iets mis" not in r.text
    assert 'data-busy="Nieuws ophalen… (kan ~20 s duren)"' in r.text
    assert '<input type="hidden" name="hours" value="24">' in r.text


def test_text_lang_is_a_simple_hint():
    from app.news.service import text_lang

    assert text_lang("Exchange hack drains $40m in Ether") == "en"
    assert text_lang("Bitcoin ETF") == "en"
    assert text_lang("De koers van bitcoin stijgt na een hack") == "nl"


# -- Plan ---------------------------------------------------------------------------------


async def test_plan_says_honestly_whether_claude_applies_it(api, monkeypatch):
    """UX-11: without Claude the page says the rules are not applied, and the message after
    saving does not promise otherwise."""
    from app.main import PLAN_SAVED, PLAN_SAVED_IDLE

    r = await api.get("/plan")
    assert "Jouw doel en regels voor Claude, plus wat de code altijd afdwingt." in r.text
    assert "Het plan waar de bot zich aan houdt" not in r.text
    assert "Claude is nu niet beschikbaar (er is geen Claude-sleutel)" in r.text
    assert "De bot koopt daarom niets tot Claude terug is" in r.text

    indicators = app.state.settings.model_copy(update={"claude_unavailable_mode": "indicators"})
    monkeypatch.setattr(app.state, "settings", indicators)
    r = await api.get("/plan")
    assert "je eigen regels hieronder worden nu niet toegepast" in r.text
    r = await api.post("/plan", data={"rules": "Niet kopen in het weekend"})
    assert flashes_from_response(r) == [{"kind": "ok", "text": PLAN_SAVED_IDLE}]

    dm = type(app.state.engine.decision_maker)
    monkeypatch.setattr(dm, "available", property(lambda self: True))
    r = await api.get("/plan")
    assert "Claude leest dit plan bij elke beslissing en elke evaluatie." in r.text
    r = await api.post("/plan", data={"rules": "Niet kopen in het weekend"})
    assert flashes_from_response(r) == [{"kind": "ok", "text": PLAN_SAVED}]


async def test_plan_separates_own_text_from_derived_rules(api):
    """UX-47, UX-12, UX-105: two sections, a clear field name, 'nog leeg' from the content,
    counters, the rules without the free description, one wording for the limits."""
    r = await api.get("/plan")
    assert "Jouw plan" in r.text and "Wat de bot afdwingt" in r.text
    assert "Wanneer de bot nooit mag handelen" in r.text and "Wat de bot niet doet" not in r.text
    assert '<span class="pill">nog leeg</span>' in r.text
    assert 'data-char-count="plan-goal-count"' in r.text and "0 van 2.000 tekens" in r.text
    assert "gerealiseerd plus het verlies op open posities" in r.text
    assert "gerealiseerd verlies koopt de bot" not in r.text
    assert "kopen en verkopen tellen allebei" in r.text and "Trades per dag" not in r.text
    assert "impact middel of groot" in r.text and "hoge impact" not in r.text
    assert '$ 0,00</span> van <span class="money">$ 100' in r.text and "dollar" not in r.text
    assert "Koop als RSI onder 35" not in r.text  # the free description is not on the plan
    assert "RSI 14 lager dan 35" in r.text and "rsi14 lager dan 35" not in r.text
    assert 'style="' not in r.text.split("<main", 1)[1]

    # Written and emptied again: 'nog leeg', not 'laatst gewijzigd'.
    await api.post("/plan", data={"goal": "Rustig"})
    assert "laatst gewijzigd" in (await api.get("/plan")).text
    await api.post("/plan", data={"goal": ""})
    assert '<span class="pill">nog leeg</span>' in (await api.get("/plan")).text


async def test_plan_error_is_next_to_its_field(api):
    r = await api.post("/plan", data={"goal": "x" * 2001})
    assert r.status_code == 400
    assert 'id="plan-goal" name="goal"' in r.text and 'aria-invalid="true"' in r.text
    assert 'id="plan-goal-error">Doel en horizon is te lang: 2001 tekens, maximaal 2000' in r.text
    assert "String should have" not in r.text


# -- Strategy -----------------------------------------------------------------------------

BASE = "https://api.bitvavo.com/v2"


def _zigzag(n: int = 300, start: float = 30000.0) -> list[list]:
    """Candles that go up 10 hours and down 10 hours: an RSI strategy trades on them."""
    out, price = [], start
    for i in range(n):
        price *= 1.004 if (i // 10) % 2 == 0 else 0.9965
        out.append(
            [
                1_700_000_000_000 + i * 3_600_000,
                str(price),
                str(price * 1.005),
                str(price * 0.995),
                str(price),
                "5",
            ]
        )
    return out


def _rsi_strategy(**changes) -> dict:
    data = {
        "name": "RSI zigzag",
        "description": "Koop bij een lage RSI.",
        "market": "BTC-EUR",
        "interval": "1h",
        "indicators": [{"id": "rsi14", "type": "rsi", "period": 14}],
        "buy": [{"left": "rsi14", "op": "<", "right": 45}],
        "sell": [{"left": "rsi14", "op": ">", "right": 55}],
        "stop_loss_pct": 5,
    }
    data.update(changes)
    return data


def _version_in(html: str) -> str:
    m = re.search(r'name="base_version" value="([0-9a-f]{16})"', html)
    assert m, "no base_version on the page"
    return m.group(1)


@respx.mock
async def test_strategy_test_redirects_and_a_reload_runs_nothing_again(api):
    """UX-16: a test lands on /strategy?result=…; reloading shows it without a new request."""
    route = respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=_zigzag()))
    body = {"action": "backtest", "strategy_json": json.dumps(_rsi_strategy()), "limit": "300"}
    r = await api.post("/strategy", data=body, headers=HTML)
    assert r.status_code == 303
    assert re.fullmatch(r"/strategy\?result=[A-Za-z0-9_-]+#resultaat", r.headers["location"])
    page = (await api.get(r.headers["location"].split("#")[0])).text
    await api.get(r.headers["location"].split("#")[0])
    assert route.call_count == 1
    # UX-43: the period, a verdict against buying and holding, labelled figures, a chart.
    assert "300 candles van 1 uur (≈ 12 dagen," in page
    assert "dan kopen en vasthouden</span>" in page and "procentpunt" in page
    assert "Afgeronde trades" in page and "Winnende trades" in page and "Grootste daling" in page
    assert (
        'role="img" aria-labelledby="curve-desc"' in page
        and "Kopen en vasthouden €\u00a01.000 →" in page
    )
    assert "Resultaat (netto)" in page
    # UX-10: the save button has its own form with the tested JSON, below the backtest.
    form = page[page.index('class="strategy-save"') - 60 :]
    assert 'data-tested-for="strategy-json"' in form and 'name="strategy_json" value="{' in form
    assert page.index('id="backtest"') < page.index('class="strategy-save"')
    assert "Geteste strategie opslaan en activeren" in page
    # The test field remembers its value.
    assert 'id="test-limit" class="input-xs" type="number" name="limit" value="300"' in page
    # An unknown or expired result says so instead of failing.
    r = await api.get("/strategy?result=bestaatniet123")
    assert r.status_code == 200 and "Dit resultaat is niet meer beschikbaar" in r.text


@respx.mock
async def test_strategy_test_on_another_market_and_scope(api):
    """UX-42: the strategy applies to all markets; 'market' only picks the backtest market."""
    eth = respx.get(f"{BASE}/ETH-EUR/candles").mock(return_value=Response(200, json=_zigzag()))
    r = await api.get("/strategy")
    assert "Geldt voor <strong>BTC-EUR, ETH-EUR</strong>" in r.text
    assert "bepaalt alleen op welke markt de backtest draait" in r.text
    body = {
        "action": "backtest",
        "strategy_json": json.dumps(_rsi_strategy()),
        "limit": "300",
        "market": "ETH-EUR",
    }
    r = await api.post("/strategy", data=body)  # not a browser: the page itself
    assert r.status_code == 200 and eth.call_count == 1
    assert "Backtest op ETH-EUR" in r.text and 'value="ETH-EUR" selected' in r.text


@respx.mock
async def test_strategy_save_confirms_when_the_backtest_gives_reason(api, monkeypatch):
    """UX-10 and UX-05: a question when live, or when the backtest lost to buying and holding
    or bought on most candles."""
    respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=_zigzag()))
    body = {"action": "backtest", "strategy_json": json.dumps(_rsi_strategy()), "limit": "300"}
    page = (await api.post("/strategy", data=body)).text
    m = re.search(
        r'class="strategy-save" data-tested-for="strategy-json"(?: data-confirm="([^"]*)")?', page
    )
    assert m is not None
    live = app.state.settings.model_copy(update={"trading_mode": "live"})
    monkeypatch.setattr(app.state, "settings", live)
    page = (await api.post("/strategy", data=body)).text
    assert 'data-confirm="LIVE · echt geld: Strategie &#39;RSI zigzag&#39; opslaan' in page
    assert 'Je handelt nu <strong class="mode-text is-live">live, met echt geld' in page


def test_save_confirm_reasons():
    from app.strategy.page import save_confirm

    bt = {
        "trades": 3,
        "open_position": False,
        "return_pct": -2.0,
        "buy_hold_return_pct": 4.0,
        "live_buy_signal_pct": 35.0,
    }
    text = save_confirm("X", ["BTC-EUR", "ETH-EUR"], bt, live=False)
    assert "slechter dan kopen en vasthouden (-2,0% tegenover +4,0%)" in text
    assert "op 35,0% van de candles een koopsignaal" in text
    good = {
        "trades": 9,
        "open_position": False,
        "return_pct": 6.0,
        "buy_hold_return_pct": 4.0,
        "live_buy_signal_pct": 5.0,
    }
    assert save_confirm("X", ["BTC-EUR"], good, live=False) is None
    assert save_confirm("X", ["BTC-EUR"], good, live=True).startswith("LIVE · echt geld: ")
    none = {"trades": 0, "open_position": False, "return_pct": 0.0, "buy_hold_return_pct": 1.0}
    assert "geen enkele trade" in save_confirm("X", ["BTC-EUR"], none, live=False)


async def test_strategy_save_from_an_old_tab_is_a_conflict(api):
    """UX-10: a save with an outdated version shows what it would undo, and only 'Toch
    overschrijven' (with the current version) goes through."""
    from app.strategy.store import load_strategy

    old_version = _version_in((await api.get("/strategy")).text)
    r = await api.post(
        "/strategy",
        data={"action": "save", "strategy_json": json.dumps(_rsi_strategy(name="Nieuw"))},
    )
    assert r.status_code == 200 and "opgeslagen en actief" in r.text
    stale = json.dumps(_rsi_strategy(name="Oud tabblad"))
    r = await api.post(
        "/strategy", data={"action": "save", "strategy_json": stale, "base_version": old_version}
    )
    assert r.status_code == 409 and "de strategie is intussen gewijzigd" in r.text
    assert (
        "Toch overschrijven" in r.text and "Laad de huidige" in r.text and "Jouw versie" in r.text
    )
    with SessionLocal() as s:
        assert load_strategy(s).name == "Nieuw"
    # From a browser: a redirect to the conflict, nothing saved.
    r = await api.post(
        "/strategy",
        data={"action": "save", "strategy_json": stale, "base_version": old_version},
        headers=HTML,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("#conflict")
    page = (await api.get(r.headers["location"].split("#")[0])).text
    current = _version_in(page)
    r = await api.post(
        "/strategy",
        data={"action": "save", "strategy_json": stale, "base_version": current},
        headers=HTML,
    )
    assert r.status_code == 303 and r.headers["location"] == "/strategy#actief"
    assert flashes_from_response(r)[0]["text"].startswith(
        "Strategie 'Oud tabblad' opgeslagen en actief op BTC-EUR, ETH-EUR"
    )


async def test_previous_strategy_is_kept_and_can_be_put_back(api):
    """UX-10: a save keeps the strategy it replaces; 'Vorige strategie terugzetten' swaps them,
    but not from a page that is out of date."""
    from app.strategy.model import DEFAULT_STRATEGY
    from app.strategy.store import load_previous, load_strategy

    await api.post(
        "/strategy", data={"action": "save", "strategy_json": json.dumps(_rsi_strategy())}
    )
    with SessionLocal() as s:
        previous, when = load_previous(s)
        assert previous.name == DEFAULT_STRATEGY.name and when is not None
    page = (await api.get("/strategy")).text
    assert "Vorige strategie terugzetten" in page and DEFAULT_STRATEGY.name in page
    version = _version_in(page)
    r = await api.post(
        "/strategy", data={"action": "restore_previous", "base_version": "0" * 16}, headers=HTML
    )
    assert r.status_code == 303 and flashes_from_response(r)[0]["kind"] == "error"
    r = await api.post(
        "/strategy", data={"action": "restore_previous", "base_version": version}, headers=HTML
    )
    assert r.status_code == 303
    assert flashes_from_response(r)[-1]["text"].startswith(
        f"Vorige strategie '{DEFAULT_STRATEGY.name}' teruggezet"
    )
    with SessionLocal() as s:
        assert load_strategy(s).name == DEFAULT_STRATEGY.name
        assert load_previous(s)[0].name == "RSI zigzag"


async def test_strategy_errors_are_dutch_and_next_to_the_editor(api):
    """UX-44: plain Dutch with where it is, under the editor, the field marked invalid."""
    good = json.dumps(_rsi_strategy(), indent=2)
    r = await api.post(
        "/strategy",
        data={"action": "backtest", "strategy_json": good.replace('"period": 14', '"period": 14,')},
    )
    assert r.status_code == 400
    assert "Regel 10, kolom" in r.text and "een komma te veel" in r.text and "^ hier" in r.text
    assert 'aria-invalid="true"' in r.text and 'aria-describedby="json-errors json-help"' in r.text
    bad = _rsi_strategy(buy=[{"left": "rsi99", "op": "<<", "right": 45}])
    r = await api.post("/strategy", data={"action": "backtest", "strategy_json": json.dumps(bad)})
    assert "Koopregel 1 → vergelijking" in r.text and "kies een van:" in r.text
    assert "Input should be" not in r.text
    r = await api.post(
        "/strategy",
        data={"action": "save", "strategy_json": json.dumps(_rsi_strategy(stop_loss_pct=80))},
    )
    assert '<strong>Stop-loss</strong> <span class="mono">stop_loss_pct</span>' in r.text
    assert "moet hoogstens 50 zijn" in r.text
    bad = _rsi_strategy(buy=[{"left": "rsi99", "op": "<", "right": 45}])
    r = await api.post("/strategy", data={"action": "save", "strategy_json": json.dumps(bad)})
    assert "onbekende reeks &#39;rsi99&#39;. Beschikbaar:" in r.text
    # A browser goes to the editor, with the text kept.
    r = await api.post(
        "/strategy", data={"action": "save", "strategy_json": json.dumps(bad)}, headers=HTML
    )
    assert r.status_code == 303 and r.headers["location"].endswith("#editor")
    page = (await api.get(r.headers["location"].split("#")[0])).text
    assert "rsi99" in page and "Ongeldige strategie, niet opgeslagen:" in page


class _FakeAssistant:
    available = True
    calls = 0

    async def propose(self, session, description, current, markets):
        from app.ai.strategy_assistant import StrategyProposal
        from app.strategy.model import Strategy

        type(self).calls += 1
        rules = _rsi_strategy(name="Claude-idee", description=current.description)
        return StrategyProposal(
            strategy=Strategy.model_validate(rules),
            explanation="## Waarom\n**Sneller** instappen.\n\n- RSI 45\n<script>x</script> [link](https://e.vil)",
        )


@respx.mock
async def test_proposal_is_shown_once_paid_for_and_rendered_safely(api, monkeypatch):
    """UX-16 and UX-28: one Claude call, however often the result is reloaded; the explanation
    as safe markdown; UX-12: a warning when the rules change but the description does not."""
    respx.get(f"{BASE}/BTC-EUR/candles").mock(return_value=Response(200, json=_zigzag()))
    monkeypatch.setattr(app.state, "assistant", _FakeAssistant())
    _FakeAssistant.calls = 0
    r = await api.post(
        "/strategy",
        data={"action": "propose", "description": "Koop sneller bij een dip", "limit": "300"},
        headers=HTML,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("#resultaat")
    url = r.headers["location"].split("#")[0]
    page = (await api.get(url)).text
    await api.get(url)
    assert _FakeAssistant.calls == 1
    assert "Voorstel van Claude: Claude-idee" in page
    assert '<div class="md"><h4>Waarom</h4><p><strong>Sneller</strong> instappen.</p>' in page
    assert "<script>x</script>" not in page and 'href="https://e.vil"' not in page
    assert "De regels veranderen, maar de omschrijving niet." in page
    assert 'data-busy="Claude denkt na… (kan ~30 s duren)"' in page
    assert "Koop sneller bij een dip" in page  # the wish stays in its field


async def test_strategy_page_without_key_and_with_a_waiting_proposal(api):
    """UX-46: no half-empty Claude card, the editor open; UX-15: a waiting proposal from an
    evaluation is linked; UX-12: the free description is a small note under the rules."""
    from app.strategy.model import DEFAULT_STRATEGY

    proposal = json.loads(DEFAULT_STRATEGY.model_dump_json()) | {"name": "Uit de evaluatie"}
    with SessionLocal() as s:
        s.add(
            Evaluation(
                model="m", period_days=7, report_nl="r", proposed_strategy=json.dumps(proposal)
            )
        )
        s.commit()
    page = (await api.get("/strategy")).text
    assert 'id="claude"' not in page and "Geavanceerd" not in page
    assert '<details class="advanced strategy-editor" open>' in page
    assert "Uit de evaluatie" in page and 'href="/trading#voorstel"' in page
    assert "Toelichting (vrije tekst)" in page
    assert page.index("Kopen als") < page.index("Toelichting (vrije tekst)")
    assert "Standaardstrategie herstellen" in page and "btn btn-danger" in page
    assert 'style="' not in page.split("<main", 1)[1]


def test_equity_curve_rebuilds_the_backtest():
    """The chart's strategy line ends where the backtest ends."""
    from app.strategy.engine import backtest
    from app.strategy.model import Strategy
    from app.strategy.page import curve_svg, equity_curves

    strategy = Strategy.model_validate(_rsi_strategy(position_size_pct=60))
    candles = [[c[0], *(float(x) for x in c[1:])] for c in _zigzag()]
    result = backtest(strategy, candles, 1000.0, 0.25)
    assert len(result.closed_trades) >= 3
    values, hold = equity_curves(candles, result, strategy.position_size_pct)
    assert values[-1] == pytest.approx(result.final_eur, rel=1e-9)
    assert hold[-1] == pytest.approx(1000 * (1 + result.buy_hold_return_pct / 100), rel=1e-9)
    curve = curve_svg(values, hold)
    assert len(curve["strategy"].split()) == 160 and curve["low"] <= min(values)
