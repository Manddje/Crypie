"""Follow-ups after the second verification round of the UX fixes: Bescherming as a block of its
own (UX-57), less explanation on Instellingen and Nieuws (UX-107) and the review's small findings
(see docs/ux-audit.md, section Opvolging)."""

import re
import time
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app import settings_store as cfg
from app.db import Position, SessionLocal, init_db
from app.labels import orders_limit_nl
from app.main import app
from tests.test_auth import PASSWORD
from tests.test_security_auth import web  # noqa: F401  (fixture)
from tests.test_security_stepup import owner  # noqa: F401  (fixture)

HTML = {"accept": "text/html,application/xhtml+xml"}


def _forget():
    with SessionLocal() as db:
        cfg.reset_overrides(db)
        db.execute(delete(Position))
        db.commit()


@pytest.fixture
async def api():
    init_db()
    _forget()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    _forget()


def _block(page: str, key: str) -> str:
    start = re.search(rf'<section class="card settings-block[^"]*" id="{key}"', page).start()
    return page[start : page.index("</section>", start)]


# -- UX-57: Bescherming is a block of its own ------------------------------------------------------


def test_protection_settings_keep_their_group_but_save_in_their_own_block():
    protection = cfg.keys_in_form("bescherming")
    assert protection == [
        "exit_check_interval_min",
        "risk_off_enabled",
        "risk_off_before_min",
        "risk_off_after_min",
        "risk_off_etf_outflow",
        "shock_mode",
        "shock_min_hours",
        "shock_max_hours",
        "shock_recheck_min",
        "shock_pause_hours",
        "event_calendar",
    ]
    assert all(cfg.SPEC_BY_KEY[k].group == "trading" for k in protection)
    handelen = cfg.keys_in_form("trading")
    assert not set(protection) & set(handelen)
    assert {"trading_mode", "markets", "trading_enabled"} <= set(handelen)
    assert cfg.group_has_sensitive("bescherming") and "bescherming" in cfg.GROUPS


async def test_saving_bescherming_does_not_send_the_mode_or_the_markets(api):
    before = app.state.settings
    form = {
        "group": "bescherming",
        "shock_pause_hours": "12",
        "risk_off_enabled": "true",
        # Not in this form: ignored, even when sent along.
        "trading_enabled": "false",
        "markets": "SOL-EUR",
        "trading_mode": "live",
    }
    r = await api.post("/settings", data=form, headers=HTML)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=bescherming#bescherming"
    now = app.state.settings
    assert now.shock_pause_hours == 12
    assert now.trading_enabled == before.trading_enabled and now.markets == before.markets
    assert now.trading_mode == "paper"
    # Saving Handelen leaves Bescherming alone (its switch is not in that form).
    r = await api.post("/settings", data={"group": "trading", "trade_interval_min": "30"})
    assert r.status_code == 303 and app.state.settings.risk_off_enabled is True
    page = (await api.get("/settings")).text
    block = _block(page, "bescherming")
    assert 'name="group" value="bescherming"' in block and 'name="shock_pause_hours"' in block
    assert 'name="trading_mode"' not in block and 'name="risk_off_enabled"' not in _block(
        page, "trading"
    )
    assert 'href="#bescherming"' in page


async def test_bescherming_has_its_own_password_field(owner):  # noqa: F811
    page = (await owner.get("/settings")).text
    block = _block(page, "bescherming")
    assert 'name="current_password"' in block and "de exit-controle" in block
    assert "vraagt wachtwoord" in block
    r = await owner.post("/settings", data={"group": "bescherming", "shock_pause_hours": "0"})
    assert r.status_code == 403 and app.state.settings.shock_pause_hours != 0
    r = await owner.post(
        "/settings",
        data={"group": "bescherming", "shock_pause_hours": "0", "current_password": PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303 and app.state.settings.shock_pause_hours == 0


async def test_the_fee_is_a_cost_and_says_why_it_asks_for_the_password(owner):  # noqa: F811
    costs = _block((await owner.get("/settings")).text, "costs")
    assert "Geen limieten" in costs
    assert "oefenorders betalen ze en de netto resultaten rekenen ermee" in costs
    # The backtest fee is no sensitive setting: it saves without the password.
    r = await owner.post(
        "/settings", data={"group": "costs", "backtest_fee_pct": "0,3"}, follow_redirects=False
    )
    assert r.status_code == 303 and app.state.settings.backtest_fee_pct == 0.3


# -- Review: a folded block shows what needs attention inside it -----------------------------------


async def test_folded_handelen_shows_an_unknown_market(api, monkeypatch):
    settings = app.state.settings.model_copy(update={"markets": ["BTC-EUR", "BTCC-EUR"]})
    monkeypatch.setattr(app.state, "settings", settings)
    app.state.market_names = (time.monotonic() + 600, {"BTC-EUR", "ETH-EUR"})
    try:
        block = _block((await api.get("/settings")).text, "trading")
    finally:
        app.state.market_names = None
    attention = re.search(r'<p class="block-attention"[^>]*>.*?</p>', block, re.S).group(0)
    assert "BTCC-EUR bestaat niet op Bitvavo" in attention and 'href="#row-markets"' in attention
    # It sits next to the summary line, before the body that folds away.
    assert block.index("block-attention") < block.index('class="settings-block-body"')
    page = (await api.get("/settings")).text
    assert "block-attention" not in page  # nothing to flag: no line


# -- Review: the closed-positions note counts closed trades only -----------------------------------


async def test_closed_positions_note_ignores_an_open_position_of_the_other_mode(api):
    with SessionLocal() as s:
        s.add(
            Position(
                mode="live",
                market="BTC-EUR",
                status="open",
                amount=0.001,
                entry_price=40000.0,
                entry_quote=40.1,
            )
        )
        s.commit()
    page = (await api.get("/trading", headers=HTML)).text
    assert "uit live:" not in page and "status=closed" not in page
    with SessionLocal() as s:
        s.add(
            Position(
                mode="live",
                market="ETH-EUR",
                status="closed",
                amount=0.01,
                entry_price=2000.0,
                entry_quote=20.0,
                exit_price=2100.0,
                exit_quote=21.0,
                pnl_eur=1.0,
                pnl_pct=5.0,
                close_reason="take_profit",
                closed_at=datetime.now(UTC),
            )
        )
        s.commit()
    page = (await api.get("/trading", headers=HTML)).text
    assert "Er is ook 1 gesloten trade uit live" in page


# -- Review: one colon in the reason why buying stopped --------------------------------------------


def test_zero_daily_maximum_reason_has_one_colon():
    reason = f"Kopen kan nu niet: {orders_limit_nl(0, 0)}."
    assert reason.count(":") == 1 and "staat op 0" in reason


# -- Review / UX-107: shorter copy, variables as secondary help ------------------------------------


async def test_login_off_banner_names_the_variable_to_switch_it_on_as_help(api):
    page = (await api.get("/news", headers=HTML)).text
    assert '<span class="mono">AUTH_ENABLED=true</span>' in page
    assert "Zet AUTH_ENABLED weer op true" not in page


async def test_settings_page_explains_in_fewer_words(api):
    page = (await api.get("/settings", headers=HTML)).text
    assert "Opslaan geldt alleen voor dit blok" not in page
    # 'Geldt alleen als … aan staat' is there for the page, but only shown while it is off.
    assert "Geldt alleen als &#39;Risk-off rond agendamomenten&#39; aan staat." in page
    from pathlib import Path

    css = (Path(__file__).parents[1] / "app/static/app.css").read_text(encoding="utf-8")
    assert ".setting:not(.is-off) .setting-dep { display: none; }" in css
    help_words = sum(len(s.help.split()) for s in cfg.SPECS)
    assert help_words < 600, help_words
    # Without a Bitvavo key the live checklist asks for one once (no advice to test it).
    assert "Nog geen Bitvavo-sleutel om te testen" not in page


async def test_news_header_is_short_and_the_range_line_only_shows_with_pages(api):
    page = (await api.get("/news", headers=HTML)).text
    header = re.search(r'<div class="page-header">.*?</div>\s*</div>', page, re.S).group(0)
    assert "Hoe telt nieuws mee?" in header and 'class="actions news-actions"' in header
    assert "op een schaal van -1 tot +1" not in page
