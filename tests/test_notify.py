"""Notifications outside the app (app/notify.py, UX-08): an ntfy topic or a webhook."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import respx
from httpx import Response
from sqlalchemy.orm import sessionmaker

from app import notify
from app.bitvavo.client import BitvavoNetworkError
from app.config import Settings
from app.db import Base, Order, Position, make_engine
from app.notify import Notifier, notify_configured, send_test, validate_notify_url
from tests.test_trading import FLAT_SERIES, FakeDecider, candles, make_engine_

NTFY = "https://ntfy.sh/crypie-geheim-topic-123"
HOOK = "https://hooks.example.nl/crypie"


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def cfg(url=NTFY, **kw):
    return SimpleNamespace(
        notify_url=url,
        notify_token=kw.get("token", ""),
        notify_format=kw.get("fmt", "auto"),
        notify_exit_fail_min=kw.get("exit_fail_min", 10),
    )


@pytest.fixture(autouse=True)
def _no_test_throttle(monkeypatch):
    monkeypatch.setattr(notify, "_last_test", 0.0)


@pytest.mark.parametrize(
    "url,problem",
    [
        ("http://ntfy.sh/topic", "https"),
        ("https://user:pass@ntfy.sh/topic", "gebruikersnaam of wachtwoord"),
        ("https://127.0.0.1/topic", "publieke hostnaam"),
        ("https://localhost/topic", "publieke hostnaam"),
        ("https://ntfy.local/topic", "lokale"),
        ("ftp://ntfy.sh/topic", "http"),
    ],
)
def test_notify_url_follows_the_feed_rules_plus_https(url, problem):
    with pytest.raises(ValueError, match=problem):
        validate_notify_url(url)
    assert not notify_configured(cfg(url))


def test_invalid_notify_url_is_ignored_at_start(caplog):
    with caplog.at_level("WARNING"):
        s = Settings(notify_url="http://ntfy.sh/geheim-topic")
    assert s.notify_url == "" and not notify_configured(s)
    assert "NOTIFY_URL is ignored" in caplog.text and "geheim-topic" not in caplog.text
    assert Settings(notify_url=f" {NTFY} ").notify_url == NTFY
    assert notify_configured(Settings(notify_url=NTFY))
    assert not notify_configured(Settings())


@respx.mock
async def test_ntfy_message_has_title_priority_and_no_secrets():
    route = respx.post(NTFY).mock(return_value=Response(200))
    n = Notifier(cfg(token="tk_abc"))
    assert n.send("unprotected", "1 positie wordt NIET bewaakt; token=geheim123 lekt niet")
    await n.drain()
    (call,) = route.calls
    req = call.request
    assert req.headers["title"] == "Crypie: positie niet bewaakt"
    assert req.headers["priority"] == "urgent"
    assert req.headers["authorization"] == "Bearer tk_abc"
    body = req.content.decode()
    assert "NIET bewaakt" in body and "geheim123" not in body
    assert n.sent_count == 1


@respx.mock
async def test_webhook_gets_json():
    route = respx.post(HOOK).mock(return_value=Response(204))
    n = Notifier(cfg(HOOK))
    n.send("live_order", "Gekocht: BTC-EUR (live, echt geld).")
    await n.drain()
    body = json.loads(route.calls[0].request.content)
    assert body["event"] == "live_order" and body["source"] == "crypie"
    assert body["message"] == "Gekocht: BTC-EUR (live, echt geld)."
    assert route.calls[0].request.headers["content-type"] == "application/json"


@respx.mock
async def test_rate_limit_per_subject_and_once_per_order():
    route = respx.post(NTFY).mock(return_value=Response(200))
    n = Notifier(cfg())
    assert n.send("exit_check_failing", "a", key="exit")
    assert not n.send("exit_check_failing", "b", key="exit")  # within the hour
    assert n.send("exit_sell_failed", "c", key="sell:1")
    assert n.send("live_order", "d", key="order:7", every=None)
    assert not n.send("live_order", "d", key="order:7", every=None)
    n._last["exit"] -= timedelta(minutes=61)
    assert n.send("exit_check_failing", "e", key="exit")
    await n.drain()
    assert route.call_count == 4


@respx.mock
async def test_hourly_cap(monkeypatch):
    respx.post(NTFY).mock(return_value=Response(200))
    monkeypatch.setattr(notify, "MAX_PER_HOUR", 3)
    n = Notifier(cfg())
    sent = [n.send("order_unknown", "x", key=f"o:{i}") for i in range(5)]
    assert sent == [True, True, True, False, False]
    await n.drain()


@respx.mock
async def test_a_failing_channel_never_raises(caplog):
    respx.post(NTFY).mock(return_value=Response(500))
    n = Notifier(cfg())
    with caplog.at_level("WARNING"):
        assert n.send("restart", "Crypie is gestart.")
        await n.drain()
    assert n.failed_count == 1 and "HTTP 500" in caplog.text
    assert "crypie-geheim-topic" not in caplog.text  # the topic is a secret


def test_off_without_url_and_outside_a_loop():
    assert not Notifier(cfg("")).send("restart", "x")
    assert not Notifier(cfg()).send("restart", "x")  # no running event loop: skipped


@respx.mock
async def test_send_test():
    route = respx.post(NTFY).mock(return_value=Response(200))
    ok, text = await send_test(cfg())
    assert ok and "Testmelding verstuurd" in text and route.call_count == 1
    ok, text = await send_test(cfg())
    assert not ok and "net een testmelding" in text  # no flood from a button
    notify._last_test = 0.0
    route.mock(return_value=Response(403))
    ok, text = await send_test(cfg())
    assert not ok and "HTTP 403" in text
    assert (await send_test(cfg("")))[0] is False
    ok, text = await send_test(cfg("http://ntfy.sh/x"))
    assert not ok and "ongeldig" in text


@respx.mock
async def test_restart_message_at_most_every_ten_minutes(session):
    route = respx.post(NTFY).mock(return_value=Response(200))
    assert Notifier(cfg()).restart(session, "Crypie is gestart in oefenmodus.")
    # A crash loop: a new process (new notifier) a minute later says nothing.
    assert not Notifier(cfg()).restart(session, "Crypie is gestart in oefenmodus.")
    await notify.asyncio.sleep(0)
    for task in list(notify.asyncio.all_tasks()):
        if task is not notify.asyncio.current_task():
            await task
    assert route.call_count == 1


# -- the engine's hooks ------------------------------------------------------------------------


class Recorder:
    exit_fail_min = 10

    def __init__(self):
        self.sent = []

    def send(self, event, text, key=None, every=None):
        self.sent.append((event, text, key))
        return True


def engine_(session):
    eng = make_engine_(session, {"BTC-EUR": candles(FLAT_SERIES)}, {"BTC-EUR": 10.0}, FakeDecider())
    eng.notifier = Recorder()
    return eng


def add_position(session, mode="paper", stop=9.0):
    pos = Position(
        mode=mode,
        market="BTC-EUR",
        amount=5.0,
        entry_price=10.0,
        entry_quote=50.0,
        stop_price=stop,
        high_price=10.0,
    )
    session.add(pos)
    session.commit()
    return pos


class DownClient:
    async def ticker_prices(self, markets):
        raise BitvavoNetworkError(TimeoutError("read timed out"))


async def test_exit_check_failing_is_reported_after_n_minutes(session):
    eng = engine_(session)
    add_position(session)
    eng.client = DownClient()
    await eng.run_exits(session)
    assert eng.notifier.sent == []  # one failure: not yet
    eng.exit_failing_since = datetime.now(UTC) - timedelta(minutes=11)
    await eng.run_exits(session)
    ((event, text, key),) = eng.notifier.sent
    assert event == "exit_check_failing" and key == "exit_check_failing"
    assert "1 open positie wordt nu niet bewaakt" in text


async def test_unprotected_position_and_failed_stop_sell_are_reported(session):
    eng = engine_(session)
    eng.broker_for_mode = lambda mode: None
    live = add_position(session, mode="live")
    await eng.run_exits(session)
    ((event, text, key),) = eng.notifier.sent
    assert event == "unprotected" and key == f"unprotected:{live.id}"
    assert "NIET bewaakt" in text and "€" not in text

    eng2 = engine_(session)
    session.delete(live)
    session.commit()
    pos = add_position(session, stop=11.0)  # price 10 is below the stop: sell, no coins
    await eng2.run_exits(session)
    events = [e for e, _, _ in eng2.notifier.sent]
    assert "exit_sell_failed" in events
    text = next(t for e, t, _ in eng2.notifier.sent if e == "exit_sell_failed")
    assert f"positie #{pos.id}" in text and "stop-loss" in text


async def test_order_with_unknown_outcome_is_reported_once_it_lingers(session):
    eng = engine_(session)
    order = Order(
        mode="paper",
        market="BTC-EUR",
        side="buy",
        order_type="market",
        status="unknown",
        created_at=datetime.now(UTC) - timedelta(minutes=6),
    )
    session.add(order)
    session.commit()
    eng.broker_for_mode = lambda mode: None
    eng.broker = SimpleNamespace(mode="live")  # the paper order cannot be settled now
    await eng.reconcile(session)
    assert [(e, k) for e, _, k in eng.notifier.sent] == [
        ("order_unknown", f"order_unknown:{order.id}")
    ]


async def test_daily_loss_limit_is_reported(session):
    eng = engine_(session)
    pos = add_position(session)
    pos.status, pos.closed_at, pos.pnl_eur = "closed", datetime.now(UTC), -80.0
    session.commit()
    await eng.run_cycle(session)
    events = [e for e, _, _ in eng.notifier.sent]
    assert events.count("daily_loss_limit") == 1
