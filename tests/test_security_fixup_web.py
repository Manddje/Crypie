"""Review fixes on login, step-up, settings, proposals and deployment defaults."""

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from app import settings_store as cfg
from app.api_docs import redoc_page, swagger_page
from app.auth.service import LoginThrottle
from app.bitvavo.broker import LiveBroker
from app.bitvavo.client import BitvavoClient
from app.config import Settings
from app.db import AuthThrottle, Evaluation, SessionLocal
from app.main import _live_reminders, app, exit_broker, live_blocker
from app.security import DOCS_CONTENT_SECURITY_POLICY, SecurityHeaders
from app.strategy.model import DEFAULT_STRATEGY
from tests.test_auth import PASSWORD, _current_code, _forget_last_code, _login, _setup
from tests.test_security_auth import web  # noqa: F401  (fixture)
from tests.test_security_stepup import owner  # noqa: F401  (fixture)
from tests.test_security_ui import _add_evaluation, api, strategy  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent

# -- F21: every way to switch protection off asks for the password ----------------------------


def test_risk_off_window_shock_pause_exit_interval_and_bot_switch_are_sensitive():
    for key in (
        "trading_enabled",
        "exit_check_interval_min",
        "risk_off_before_min",
        "risk_off_after_min",
        "event_calendar",
        "shock_pause_hours",
    ):
        assert key in cfg.SENSITIVE_KEYS, key
        assert cfg.SPEC_BY_KEY[key].group == "trading"  # that form has the password field


async def test_collapsing_the_risk_off_window_needs_the_password(owner):  # noqa: F811
    body = {"risk_off_before_min": 0, "risk_off_after_min": 0}
    r = await owner.put("/api/settings", json=body)
    assert r.status_code == 403
    assert app.state.engine.risk_off_before_min != 0
    for body in ({"shock_pause_hours": 0}, {"exit_check_interval_min": 60}):
        assert (await owner.put("/api/settings", json=body)).status_code == 403
    r = await owner.put("/api/settings", json={**body, "current_password": PASSWORD})
    assert r.status_code == 200 and app.state.settings.exit_check_interval_min == 60


async def test_runtime_fee_does_not_make_every_save_sensitive(owner):  # noqa: F811
    # At startup the real Bitvavo fee replaces TRADING_FEE_PCT (runtime only).
    app.state.settings = app.state.settings.model_copy(update={"trading_fee_pct": 0.15})
    r = await owner.put("/api/settings", json={"news_enabled": False})
    assert r.status_code == 200, r.text
    assert app.state.settings.trading_fee_pct == 0.15  # not swapped back by an unrelated save
    assert app.state.engine.fee_pct == 0.15
    r = await owner.put("/api/settings", json={"trading_fee_pct": 0.3})
    assert r.status_code == 403  # a real change of the fee still is sensitive


# -- regression: protective sells of live positions do not need the login ---------------------


def test_exit_broker_keeps_live_positions_protected_without_the_login():
    client = BitvavoClient("key", "secret", "https://api.bitvavo.com/v2")
    env = Settings(auth_enabled=False, live_trading_ack=True)
    state = SimpleNamespace(env=env, client=client, settings=env)
    fake_app = SimpleNamespace(state=state)
    assert isinstance(exit_broker(fake_app, "live"), LiveBroker)
    assert "AUTH_ENABLED" in live_blocker(env, client)  # opening trades still needs it
    assert live_blocker(env, client, for_exits=True) is None
    no_ack = Settings(auth_enabled=False, live_trading_ack=False)
    fake_app.state.env = no_ack
    assert exit_broker(fake_app, "live") is None


# -- F01: new recovery codes need a current code ------------------------------------------------


async def test_new_recovery_codes_need_password_and_code(web):  # noqa: F811
    secret, codes = await _setup(web)
    r = await web.post("/account", data={"action": "recovery", "password": PASSWORD})
    assert r.status_code == 401 and "code klopt niet" in r.text
    r = await web.post(
        "/account", data={"action": "recovery", "password": PASSWORD, "code": "000000"}
    )
    assert r.status_code == 401
    _forget_last_code()
    r = await web.post(
        "/account",
        data={"action": "recovery", "password": PASSWORD, "code": _current_code(secret)},
    )
    assert r.status_code == 200 and "Herstelcodes" in r.text
    page = (await web.get("/account")).text
    assert page.count('name="code"') == 2  # recovery and re-link forms


# -- F04: account throttle rows survive a full table; failures are shown -----------------------


def test_full_throttle_table_never_evicts_the_account_counter():
    t = LoginThrottle(max_failures=5, decay_seconds=10_000, max_entries=5)
    with SessionLocal() as db:
        db.query(AuthThrottle).delete()
        db.commit()
        t.record_failure(db, "mfa:acct", now=0)
        t.record_failure(db, "mfa:acct", now=1)
        for i in range(20):
            t.record_failure(db, f"ip:2001:db8:{i}::/64", now=10 + i)
        assert t.failures(db, "mfa:acct", now=50) == 2
        assert db.query(AuthThrottle).count() <= 5
        db.query(AuthThrottle).delete()
        db.commit()


async def test_wrong_codes_before_a_login_are_shown_on_the_account_page(web):  # noqa: F811
    secret, _ = await _setup(web)
    await web.post("/logout")
    assert (await _login(web)).status_code == 303
    for _ in range(2):
        assert (await web.post("/login/mfa", data={"code": "000000"})).status_code == 401
    _forget_last_code()
    assert (await web.post("/login/mfa", data={"code": _current_code(secret)})).status_code == 303
    page = (await web.get("/account")).text
    assert "Voor je laatste login is 2 keer een verkeerde code" in page
    # A clean login clears the notice.
    await web.post("/logout")
    await _login(web)
    _forget_last_code()
    await web.post("/login/mfa", data={"code": _current_code(secret)})
    assert "verkeerde code" not in (await web.get("/account")).text


def _proxy_scope(scheme: str, proto: str | None) -> dict:
    headers = [(b"x-forwarded-proto", proto.encode())] if proto else []
    return {"type": "http", "scheme": scheme, "client": ("fd00::5", 1), "headers": headers}


def test_untrusted_proxy_is_reported_once(caplog):
    middleware = SecurityHeaders(app=None)
    with caplog.at_level(logging.WARNING):
        middleware._check_proxy(_proxy_scope("https", "https"))  # trusted: fine
        middleware._check_proxy(_proxy_scope("http", None))  # no proxy at all
        assert "FORWARDED_ALLOW_IPS" not in caplog.text
        middleware._check_proxy(_proxy_scope("http", "https"))
        middleware._check_proxy(_proxy_scope("http", "https"))
    warnings = [r for r in caplog.records if "not in FORWARDED_ALLOW_IPS" in r.getMessage()]
    assert len(warnings) == 1 and "fd00::5" in warnings[0].getMessage()


def test_default_trusted_proxies_include_ipv6_ula_and_no_user_site():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    assert "fc00::/7" in re.search(r"FORWARDED_ALLOW_IPS=(\S+)", dockerfile).group(1)
    assert "fc00::/7" in re.search(r"FORWARDED_ALLOW_IPS:-([^}]*)\}", compose).group(1)
    assert "PYTHONNOUSERSITE=1" in dockerfile and "--no-create-home" in dockerfile


def test_ci_audits_weekly_and_dependabot_groups_the_locks():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(r"schedule:\s*\n\s*- cron:", ci)
    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    assert "groups:" in dependabot


# -- F17: a banner on every page while the login is off -----------------------------------------


async def test_every_page_warns_while_the_login_is_off(api):  # noqa: F811
    for path in ("/", "/settings", "/news"):
        assert "De login staat uit (AUTH_ENABLED=false)" in (await api.get(path)).text, path


async def test_no_banner_with_the_login_on(web):  # noqa: F811
    await _setup(web)
    assert "De login staat uit" not in (await web.get("/settings")).text


# -- F25: the API docs load pinned scripts with SRI ---------------------------------------------


def test_api_docs_pages_pin_versions_and_check_integrity():
    swagger = swagger_page("Crypie", "/openapi.json").body.decode()
    redoc = redoc_page("Crypie", "/openapi.json").body.decode()
    for page in (swagger, redoc):
        for tag in re.findall(r"<(?:script|link)[^>]+cdn\.jsdelivr\.net[^>]*>", page):
            assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag, tag
            assert re.search(r"@\d+\.\d+\.\d+/", tag), tag
        assert "<script>" not in page  # no inline script
    assert (
        "'unsafe-inline'" not in DOCS_CONTENT_SECURITY_POLICY.split("script-src")[1].split(";")[0]
    )


# -- F02 / F21 / F66: environment values are clamped, never fatal -------------------------------


def test_environment_values_out_of_range_are_clamped():
    assert Settings(auth_session_hours=0).auth_session_hours == 1
    assert Settings(auth_session_hours=100_000).auth_session_hours == 168
    assert Settings(news_max_age_hours=5000).news_max_age_hours == 720
    s = Settings(max_trade_eur_ceiling=0, max_trades_per_day_ceiling=-1)
    assert s.max_trade_eur_ceiling == 1000.0 and s.max_trades_per_day_ceiling == 50
    low = cfg.apply_ceilings(Settings(max_trade_eur_ceiling=5, min_trade_eur=10))
    assert low.max_trade_eur == 5 and low.min_trade_eur == 5
    assert cfg.check_consistency(low) == {}


# -- F82 / F59: live-mode reminders -------------------------------------------------------------


def test_live_reminders_for_capital_and_key():
    env = Settings()
    reminders = _live_reminders(env, env)
    assert any("LIVE_CAPITAL_EUR" in r for r in reminders)
    assert any("BITVAVO_KEY_RESTRICTED" in r for r in reminders)
    done = Settings(live_capital_eur=500, bitvavo_key_restricted=True)
    assert _live_reminders(done, done) == []


# -- F35 / F76: evaluator proposals may not widen the stop or buy on nearly every candle -------


def _proposal(**overrides) -> dict:
    return json.loads(strategy(**overrides).model_dump_json())


async def test_trailing_stop_that_starts_late_is_no_stop(api):  # noqa: F811
    late = _add_evaluation(_proposal(stop_loss_pct=None, trailing_start_pct=400))
    r = await api.post(f"/api/evaluations/{late}/apply")
    assert r.status_code == 400 and "geen stop vanaf de instap" in r.json()["detail"]
    r = await api.put("/api/strategy", json=_proposal(stop_loss_pct=None, trailing_start_pct=2))
    assert r.status_code == 400


async def test_proposal_that_widens_the_stop_is_refused(api):  # noqa: F811
    wider = _add_evaluation(_proposal(stop_loss_pct=50))
    r = await api.post(f"/api/evaluations/{wider}/apply")
    assert r.status_code == 400 and "stop ruimer (5% -> 50%)" in r.json()["detail"]
    page = (await api.get("/trading")).text
    assert "stop ruimer" in page and "Dit voorstel kan niet worden toegepast" in page
    # The owner can still save it deliberately on the strategy page.
    assert (await api.put("/api/strategy", json=_proposal(stop_loss_pct=50))).status_code == 200
    tighter = _add_evaluation(_proposal(name="Strakker", stop_loss_pct=3))
    assert (await api.post(f"/api/evaluations/{tighter}/apply")).status_code == 200


async def test_proposal_that_buys_on_most_candles_is_refused(api):  # noqa: F811
    ev_id = _add_evaluation(_proposal(name="Vaak"))
    with SessionLocal() as s:
        s.get(Evaluation, ev_id).proposal_backtest = json.dumps(
            {"proposed": {"live_buy_signal_pct": 80.0}}
        )
        s.commit()
    r = await api.post(f"/api/evaluations/{ev_id}/apply")
    assert r.status_code == 400 and "80% van de candles" in r.json()["detail"]


# -- F21: https-only feeds; stored http feeds still load ----------------------------------------


def test_stored_http_feed_still_loads():
    with SessionLocal() as s:
        cfg.save_overrides(s, {"news_rss_feeds": ["https://a.example/rss"]})
        row = s.get(cfg.Setting, cfg.CONFIG_PREFIX + "news_rss_feeds")
        row.value = json.dumps(["http://old.example/rss"])
        s.commit()
        try:
            assert cfg.effective_settings(s).news_rss_feeds == ["http://old.example/rss"]
        finally:
            cfg.reset_overrides(s)


async def test_api_refuses_plain_http_feeds(api):  # noqa: F811
    r = await api.put("/api/settings", json={"news_rss_feeds": ["http://example.org/feed"]})
    assert r.status_code == 422 and "gebruik https://" in r.json()["detail"]["news_rss_feeds"]


def test_default_strategy_passes_the_new_checks():
    assert DEFAULT_STRATEGY.entry_stop_pct() == 5


async def test_docs_app_routes_stay_off(api):  # noqa: F811
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await api.get(path)).status_code == 404


async def test_banner_client_without_lifespan():
    # Pages rendered before the lifespan set app.state.auth do not crash on the banner check.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/health")).status_code == 200
