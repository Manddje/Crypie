"""Wording sweep (UX audit, sweep 2): one glossary, one sentence per rule, the mode and the time
zone said everywhere, and money in one notation (UX-05, UX-09, UX-30..32, UX-104..107)."""

import re
from datetime import UTC, datetime, timedelta

from app.ai.decision_maker import TradeDecision
from app.db import NewsItem, Position, SessionLocal
from app.main import app
from app.trading import risk
from app.web import KILL_SWITCH_NL, eur_short, hours_nl, templates
from tests.test_trading import FLAT_SERIES, FakeDecider, candles, make_engine_, session  # noqa: F401
from tests.test_ux_dashboard import api  # noqa: F401 - the overview fixture (prices mocked)

PAGES = ("/", "/trading", "/journal", "/plan", "/strategy", "/news", "/settings")
# Words the glossary replaced; none of them may reach a page (UX-106, UX-09).
OLD_WORDS = (
    "Winstkans",
    "cyclussen",
    "kill switch",
    "trades per dag",
    "per kant",
    "taker-fee",
    "van hoge impact",
    "Pauzeren</button>",
    "Bitvavo-limiet resterend",
)


def _text(html: str) -> str:
    """The visible text of a page, roughly: tags and scripts out."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


async def test_no_page_uses_the_old_words(api):  # noqa: F811
    for path in PAGES:
        page = (await api.get(path)).text
        for word in OLD_WORDS:
            assert word not in page, (path, word)


async def test_the_kill_switch_is_explained_once_beside_the_status(api):  # noqa: F811
    """UX-09/UX-105: one sentence for what the noodstop does. While paused, the status says it
    and the kill switch adds only what follows from it."""
    help_ = KILL_SWITCH_NL["help"]
    with SessionLocal() as s:
        risk.set_paused(s, True)
    try:
        for path, start, end in (
            ("/", 'class="hero dash-hero"', 'id="open-posities"'),
            ("/trading", 'class="hero trading-hero"', 'id="laatste-cyclus"'),
        ):
            page = (await api.get(path)).text
            hero = page[page.index(start) : page.index(end)]
            assert hero.count(help_) == 1, path
            assert KILL_SWITCH_NL["detail"] in hero and KILL_SWITCH_NL["resume"] in hero
        body = (await api.get("/api/status")).json()
        assert body["health"]["detail"] == help_
    finally:
        with SessionLocal() as s:
            risk.set_paused(s, False)
    # Not paused: the sentence sits under the kill switch button.
    page = (await api.get("/")).text
    assert page.count(help_) == 1


async def test_the_hard_limits_read_the_same_on_handelen_and_the_plan(api):  # noqa: F811
    """UX-105: one macro writes the hard limits, with the labels of Instellingen."""

    def rows(page: str) -> list[str]:
        start = page.index("<dt>Per order</dt>")
        end = page.index("Afkoelperiode na stop-loss")
        block = page[start : page.index("</dd>", end)]
        return [_text(r).strip() for r in re.findall(r"<d[td]>.*?</d[td]>", block, flags=re.S)]

    trading = rows((await api.get("/trading")).text)
    plan = rows((await api.get("/plan")).text)
    assert trading == plan
    labels = trading[0::2]
    assert labels == [
        "Per order",
        "Maximaal in crypto",
        "Dagverlieslimiet",
        "Maximum aantal orders per dag",
        "Afkoelperiode na stop-loss",
    ]
    assert trading[1].startswith("minimaal € ") and "maximaal € " in trading[1]


async def test_fees_are_per_order_everywhere(api):  # noqa: F811
    for path in ("/", "/trading", "/plan"):
        page = (await api.get(path)).text
        assert "per kant" not in page
    assert "% per order;" in (await api.get("/plan")).text


async def test_the_plan_names_the_models_as_people_say_them(api):  # noqa: F811
    page = (await api.get("/plan")).text
    assert "claude-fable-5-1" not in page and "Claude Fable 5.1" in page
    assert re.search(r"Claude Fable 5\.1, elke \d+ (dag|dagen|uur)", page)


async def test_the_strategy_page_shows_readable_conditions(api):  # noqa: F811
    """UX-104: the rules read like on Handelen and the plan; the chips map the JSON ids."""
    page = (await api.get("/strategy")).text
    rules = page[page.index("Kopen als") : page.index("strategy-chips")]
    assert "RSI 14 lager dan 35" in rules and "rsi14 lager dan" not in rules
    assert "onder de instapkoers zakt" in rules
    assert '<span class="mono">rsi14</span> = RSI 14' in page


async def test_the_getting_started_step_for_live_says_it_in_plain_words(api):  # noqa: F811
    page = (await api.get("/")).text
    if 'id="aan-de-slag"' in page:
        assert "AUTH_ENABLED staat op false" not in page
        assert "Zet de sleutel in de serveromgeving:" in page


def test_the_wins_say_netto_or_bruto():
    """UX-106: 'Winnende trades' is netto everywhere, the backtest included (one meaning per
    name)."""
    for name in ("dashboard.html", "trading.html", "journal.html", "strategy.html"):
        source = templates.env.loader.get_source(templates.env, name)[0]
        assert "met netto winst" in source, name
        assert "met bruto winst" not in source, name


def test_backtest_wins_are_net_of_fees():
    from app.strategy.engine import BacktestResult, Trade

    small_gain = Trade(entry_time=0, entry_price=100.0, exit_time=1, exit_price=100.4)
    result = BacktestResult(
        start_eur=1000,
        final_eur=1000,
        return_pct=0,
        buy_hold_return_pct=0,
        max_drawdown_pct=0,
        trades=[small_gain],
        fee_pct=0.25,
    )
    assert small_gain.pnl_pct > 0 and small_gain.net_pnl_pct(0.25) < 0
    assert result.wins == 0 and result.win_rate_pct == 0


def test_limits_and_intervals_have_one_notation():
    assert eur_short(100) == "€ 100" and eur_short(7.5) == "€ 7,50"
    assert eur_short(1000) == "€ 1.000" and eur_short(None) == "–"
    assert hours_nl(168) == "7 dagen" and hours_nl(24) == "1 dag" and hours_nl(12) == "12 uur"
    assert hours_nl(None) == "–"


async def test_rules_decisions_count_news_with_the_impact_scale(session):  # noqa: F811
    """UX-105: the engine writes 'impact middel of groot' (impact 2 and up), as Nieuws does."""
    decider = FakeDecider(
        TradeDecision(action="hold", confidence=1, amount_quote=0, reasoning_nl="x")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    session.add(
        NewsItem(
            source="rss",
            source_name="CoinDesk",
            url="https://coindesk/x",
            title="Something",
            published_at=datetime.now(UTC) - timedelta(hours=1),
            assessed=True,
            relevant=True,
            sentiment=0.2,
            impact=2,
            horizon="days",
            coins="BTC",
            event_key="x",
        )
    )
    session.commit()
    (result,) = await engine.run_cycle(session)
    assert "met impact middel of groot" in result.reason and "hoge impact" not in result.reason


async def test_a_news_sell_held_by_the_kill_switch_says_so(session):  # noqa: F811
    """A sell on news alone waits during the noodstop; the refusal must not speak of buying."""
    decider = FakeDecider(
        TradeDecision(action="sell", confidence=0.7, amount_quote=0, reasoning_nl="Hack.")
    )
    engine = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, decider)
    await engine.broker.market_order(session, "BTC-EUR", "buy", 50, reason="setup")
    session.add(Position(mode="paper", market="BTC-EUR", amount=5, entry_price=10, entry_quote=50))
    for outlet in ("CoinDesk", "The Block"):
        session.add(
            NewsItem(
                source="rss",
                source_name=outlet,
                url=f"https://{outlet}/hack",
                title="Exchange hacked",
                published_at=datetime.now(UTC) - timedelta(hours=1),
                assessed=True,
                relevant=True,
                sentiment=-0.9,
                impact=3,
                horizon="days",
                coins="ALL",
                event_key="exchange-hack",
            )
        )
    session.commit()
    risk.set_paused(session, True)
    (result,) = await engine.run_cycle(session)
    assert not result.executed
    assert "noodstop" in result.reason and "verkoop alleen op negatief nieuws" in result.reason
    assert "kopen is gepauzeerd" not in result.reason


def test_the_app_is_imported():  # keeps the filters registered by app.main in this module
    assert app is not None
    assert "model_name" in templates.env.filters and "indicator_labels" in templates.env.filters
