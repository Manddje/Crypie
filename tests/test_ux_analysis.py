"""The analysis pages after the UX audit: Journaal, Nieuws, Tradingplan and Strategie.

Journal: the default filter form works ('alles' is days=0, an empty field is no filter), close
reasons are Dutch and filterable, manual trades are not Claude's, active filters are summed up
with links that remove them, and the other mode can be shown. News: no silent cap (paging and
counts over the whole period), no red error after a refresh without a key, readable details and
English titles marked as English. Plan and strategy: see the sections below.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.db import Decision, NewsItem, Order, Position, SessionLocal, Setting, init_db
from app.flash import flashes_from_response
from app.main import app
from app.trading import risk
from app.trading.journal import JournalFilter, journal_entries, journal_totals, order_source

HTML = {"accept": "text/html,application/xhtml+xml"}


def _clean():
    with SessionLocal() as s:
        for table in (Decision, Order, Position, NewsItem):
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
    chips = dict(re.findall(r'class="chip-remove" href="([^"]+)">([^<]+)<', r.text))
    assert chips == {
        "/journal?result=win&amp;days=30": "Markt: ETH-EUR",
        "/journal?market=ETH-EUR&amp;days=30": "Resultaat: winst",
        "/journal?market=ETH-EUR&amp;result=win": "Periode: laatste 30 dagen",
    }
    assert "Resultaat van de selectie" in r.text and "Alle filters wissen" in r.text
    assert '<span class="pill">3 actief</span>' in r.text
    # The filter block folds away on a phone (analysis.js) and the note form returns here.
    assert "data-collapse-mobile" in r.text and "/static/analysis.js" in r.text
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
    assert re.search(r'Telt mee<span class="count-badge">1</span>', r.text)
    assert re.search(r'Telt niet mee<span class="count-badge">200</span>', r.text)
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
    assert "rsi14 lager dan 35" in r.text
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
