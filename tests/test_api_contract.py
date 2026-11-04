"""The JSON API's contract: one error shape with a code, status codes that mean one thing, 202
for an order whose outcome is unknown, Idempotency-Key, UTC times, cursor pages with a Link
header, PATCH for settings, and an OpenAPI schema of the API alone."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.bitvavo.broker import OrderUncertain
from app.bitvavo.client import BitvavoError
from app.db import Evaluation, NewsItem, Order, Position, SessionLocal
from app.main import app
from tests.test_auth import _setup
from tests.test_mcp import TOKEN, _text, call
from tests.test_security_auth import web  # noqa: F401  (fixture: login on)
from tests.test_ux_dashboard import api  # noqa: F401  (fixture: login off, prices mocked)

LINK_RE = re.compile(r'<([^>]+)>; rel="next"')


def _next(r) -> str | None:
    match = LINK_RE.search(r.headers.get("link", ""))
    return match.group(1) if match else None


async def _all_pages(client, url: str, key: str | None = None) -> list[dict]:
    """Every item of a paged list, following the Link headers (``key``: the list in a dict)."""
    items: list[dict] = []
    for _ in range(100):
        r = await client.get(url)
        assert r.status_code == 200, r.text
        items += r.json()[key] if key else r.json()
        url = _next(r)
        if url is None:
            return items
    raise AssertionError("no last page")


# -- orders ----------------------------------------------------------------------------------


async def test_an_order_with_an_unknown_outcome_is_accepted_not_refused(api, monkeypatch):  # noqa: F811
    with SessionLocal() as s:
        order = Order(
            mode="paper",
            market="BTC-EUR",
            side="buy",
            order_type="market",
            amount_quote=20.0,
            status="unknown",
        )
        s.add(order)
        s.commit()

    async def manual_order(*args, **kwargs):
        raise OrderUncertain(order, "timeout")

    monkeypatch.setattr(app.state.engine, "manual_order", manual_order)
    body = {"market": "BTC-EUR", "side": "buy", "amount_quote": 20}
    r = await api.post("/api/orders", json=body)
    assert r.status_code == 202, r.text
    assert r.headers["location"] == f"/api/orders/{order.id}"
    assert r.json()["status"] == "unknown" and "plaats hem niet opnieuw" in r.json()["message"]
    followed = await api.get(r.headers["location"])
    assert followed.status_code == 200 and followed.json()["id"] == order.id

    # Over MCP it is a result, not "Crypie weigerde dit".
    answer = await call(api, "place_order", body)
    assert "isError" not in answer["result"], _text(answer)
    assert answer["result"]["structuredContent"]["status"] == "unknown"
    answer = await call(api, "order", {"order_id": order.id})
    assert answer["result"]["structuredContent"]["id"] == order.id


async def test_an_idempotency_key_places_an_order_once(api):  # noqa: F811
    buy = {"market": "ETH-EUR", "side": "buy", "amount_quote": 20}
    key = {"Idempotency-Key": f"test-{uuid4()}"}
    r = await api.post("/api/orders", json=buy, headers=key)
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]
    assert r.headers["location"] == f"/api/orders/{order_id}"
    # Times carry their zone: JavaScript would read a bare time as local time.
    assert r.json()["created_at"].endswith("+00:00")
    assert (await api.get(f"/api/orders/{order_id}")).json()["created_at"].endswith("+00:00")

    again = await api.post("/api/orders", json=buy, headers=key)
    assert again.status_code == 409 and again.json()["code"] == "order_resent"
    assert [o["id"] for o in (await api.get("/api/orders?mode=paper")).json()].count(order_id) == 1

    # A refusal before anything was sent leaves the key free for the corrected order.
    sell_key = f"test-{uuid4()}"
    bad = {"market": "ETH-EUR", "side": "sell", "amount_quote": 1, "idempotency_key": sell_key}
    r = await api.post("/api/orders", json=bad)
    assert r.status_code == 422 and r.json()["code"] == "order_refused"
    sell = bad | {"amount_quote": 20}
    assert (await api.post("/api/orders", json=sell)).status_code == 201
    r = await api.post("/api/orders", json=sell)  # the key as a body field counts too
    assert r.status_code == 409 and r.json()["code"] == "order_resent"

    r = await api.get("/api/orders/999999")
    assert r.status_code == 404 and r.json()["code"] == "order_not_found"


# -- errors ----------------------------------------------------------------------------------


async def test_validation_errors_name_the_fields_and_never_echo_the_input(api):  # noqa: F811
    secret = "geheim-wachtwoord-123"
    r = await api.post(
        "/api/orders", json={"market": "BTC-EUR", "side": "short", "current_password": secret}
    )
    assert r.status_code == 422 and r.json()["code"] == "invalid_input"
    assert set(r.json()["fields"]) == {"side", "amount_quote"}
    assert r.json()["fields"]["amount_quote"] == "ontbreekt" and secret not in r.text
    assert isinstance(r.json()["detail"], str)

    r = await api.post(
        "/api/orders", content=b"{kapot", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400 and r.json()["code"] == "invalid_json"
    r = await api.get("/api/orders", params={"cursor": "kapot"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_cursor"
    r = await api.get("/api/candles/XRP-EUR")
    assert r.status_code == 404 and r.json()["code"] == "market_not_tracked"


async def test_no_claude_key_is_the_same_503_everywhere(api):  # noqa: F811
    calls = (
        ("/api/strategy/propose", {"description": "koop bij een dip"}),
        ("/api/trading/consult", {"market": "BTC-EUR"}),
        ("/api/evaluations", None),
    )
    for path, body in calls:
        r = await api.post(path, json=body)
        assert r.status_code == 503 and r.json()["code"] == "claude_unavailable", path
        assert "ANTHROPIC_API_KEY" in r.json()["detail"]


async def test_a_bug_behind_prices_is_a_500_not_a_bitvavo_outage(api, monkeypatch):  # noqa: F811
    async def broken(markets):
        raise KeyError("intern-veld")

    monkeypatch.setattr(app.state.client, "ticker_prices", broken)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/prices")
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
    assert "intern-veld" not in r.text

    async def down(markets):
        raise BitvavoError(0, {"error": "time-out"})

    monkeypatch.setattr(app.state.client, "ticker_prices", down)
    r = await api.get("/api/prices")
    assert r.status_code == 502 and r.json()["code"] == "bitvavo_unreachable"


async def test_refusals_before_the_app_keep_the_api_shape(api):  # noqa: F811
    cross = {"origin": "https://elders.example", "sec-fetch-site": "cross-site"}
    r = await api.post("/api/trading/pause", json={"paused": True}, headers=cross)
    assert r.status_code == 403 and r.json()["code"] == "cross_origin"
    r = await api.post("/trading/action", data={"action": "cycle"}, headers=cross)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/plain")
    r = await api.put(
        "/api/plan", content=b"x" * (1024 * 1024 + 1), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["code"] == "too_large"


@pytest.mark.parametrize("web", [{"API_TOKEN": TOKEN}], indirect=True)
async def test_a_wrong_token_on_the_api_names_metadata_that_exists(web):  # noqa: F811
    await _setup(web)
    r = await web.get("/api/status", headers={"authorization": "Bearer fout-" + "y" * 40})
    assert r.status_code == 401 and r.json()["code"] == "invalid_token"
    url = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"]).group(1)
    assert (await web.get(urlsplit(url).path)).status_code == 200


# -- pages -----------------------------------------------------------------------------------


async def test_lists_are_paged_with_a_cursor_in_the_link_header(api):  # noqa: F811
    with SessionLocal() as s:
        for i in range(3):
            s.add(
                Order(
                    mode="paper",
                    market="BTC-EUR",
                    side="buy",
                    order_type="market",
                    status="filled",
                    reason=f"pagina-{i}",
                )
            )
        s.commit()
    r = await api.get("/api/orders", params={"limit": 2})
    assert len(r.json()) == 2 and _next(r).startswith("/api/orders?limit=2&cursor=")
    orders = await _all_pages(api, "/api/orders?limit=2")
    ids = [o["id"] for o in orders]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == len(ids)
    assert {f"pagina-{i}" for i in range(3)} <= {o["reason"] for o in orders}

    # MCP passes the cursor on, so a conversation can read further too.
    answer = await call(api, "orders", {"limit": 1})
    assert 'roep orders opnieuw aan met cursor="' in _text(answer)


async def test_pages_ordered_by_a_moment_do_not_skip_ties(api):  # noqa: F811
    moment = datetime.now(UTC) - timedelta(hours=1)
    with SessionLocal() as s:
        closed = [
            Position(
                mode="paper",
                market="BTC-EUR",
                amount=1.0,
                entry_price=100.0,
                entry_quote=100.0,
                status="closed",
                closed_at=moment,
            )
            for _ in range(3)
        ]
        news = [
            NewsItem(
                source="test",
                source_name="Test",
                url=f"https://example.org/{uuid4()}",
                title=f"Gelijktijdig {i}",
                published_at=moment,
            )
            for i in range(3)
        ]
        s.add_all(closed + news)
        s.commit()
        closed_ids = [p.id for p in closed]
        news_ids = [n.id for n in news]

    pages = await _all_pages(api, "/api/positions?limit=1", key="closed")
    seen = [p["id"] for p in pages if p["id"] in closed_ids]
    assert seen == sorted(closed_ids, reverse=True)
    articles = await _all_pages(api, "/api/news?limit=1&hours=2")
    seen = [a["id"] for a in articles if a["id"] in news_ids]
    assert seen == sorted(news_ids, reverse=True)

    answer = await call(api, "positions", {"limit": 1})
    assert answer["result"]["structuredContent"]["next_cursor"]

    journal = (await api.get("/api/journal", params={"limit": 1})).json()
    assert journal["truncated"] is True and len(journal["entries"]) == 1


async def test_evaluations_are_paged_and_can_be_read_one_by_one(api):  # noqa: F811
    with SessionLocal() as s:
        evs = [Evaluation(model="test", period_days=7, report_nl=f"rapport {i}") for i in range(2)]
        s.add_all(evs)
        s.commit()
        ids = [e.id for e in evs]
    r = await api.get("/api/evaluations", params={"limit": 1})
    assert len(r.json()) == 1 and _next(r)
    r = await api.get(f"/api/evaluations/{ids[0]}")
    assert r.status_code == 200 and r.json()["report_nl"] == "rapport 0"
    r = await api.get("/api/evaluations/999999")
    assert r.status_code == 404 and r.json()["code"] == "evaluation_not_found"


# -- settings and docs -----------------------------------------------------------------------


async def test_settings_change_with_patch(api):  # noqa: F811
    try:
        r = await api.patch("/api/settings", json={"max_trades_per_day": 3})
        assert r.status_code == 200 and r.json()["values"]["max_trades_per_day"] == 3
        r = await api.patch("/api/settings", json={"max_trades_per_day": "veel"})
        assert r.status_code == 422 and r.json()["code"] == "invalid_settings"
        assert "max_trades_per_day" in r.json()["fields"]
    finally:
        await api.delete("/api/settings/max_trades_per_day")


def test_openapi_documents_the_api_and_its_error_shape():
    schema = app.openapi()
    paths = schema["paths"]
    assert paths and all(p.startswith("/api/") or p == "/health" for p in paths), sorted(paths)
    assert paths["/api/settings"]["put"]["deprecated"] is True and "patch" in paths["/api/settings"]
    post = paths["/api/orders"]["post"]
    assert {"201", "202", "422"} <= set(post["responses"])
    assert any(p["name"] == "Idempotency-Key" for p in post["parameters"])
    assert "ErrorOut" in schema["components"]["schemas"]
    assert "properties" in schema["components"]["schemas"]["SettingsIn"]
